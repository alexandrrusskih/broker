// The files a box must not share with the machine, and the ones it must.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { root, temp, engine } = require("./helpers");

test("the links to the machine's databases are taken out of a profile", async (t) => {
  const dir = await temp(t);
  const shared = path.join(dir, ".codex");
  const profile = path.join(dir, ".codex-ar");
  const databases = path.join(dir, "db", "w1");
  await fs.mkdir(shared, { recursive: true });
  await fs.mkdir(profile, { recursive: true });
  await fs.writeFile(path.join(shared, "logs_2.sqlite"), "the machine's own");
  // What the harness leaves behind on every start.
  await fs.symlink(path.join(shared, "logs_2.sqlite"), path.join(profile, "logs_2.sqlite"));
  // And something real, which is nobody's business to remove.
  await fs.writeFile(path.join(profile, "state_5.sqlite"), "real file");

  const out = engine(`
import json, os
from broker.providers import codex
codex.CANONICAL_HOME = ${JSON.stringify(shared)}
env = {"CODEX_HOME": ${JSON.stringify(profile)},
       "CODEX_SQLITE_HOME": ${JSON.stringify(databases)}}
codex.harness_env(env)
print(json.dumps(sorted(os.listdir(${JSON.stringify(profile)}))))
`, { BROKER_CONFIG_DIR: dir, HOME: dir });

  // The link goes: it points at the database every other process on this
  // machine has open, and a start is refused while any of them holds it.
  // The real file stays: it is data, not a pointer at someone else's.
  assert.deepEqual(JSON.parse(out), ["state_5.sqlite"]);
  assert.equal(await fs.readFile(path.join(shared, "logs_2.sqlite"), "utf8"),
    "the machine's own", "and the machine's own database is untouched");
});

test("a database the harness invents in a profile becomes everyone's", () => {
  // codex added state_5.sqlite, and later thread_history_1.sqlite, at a moment
  // when the canonical home had no such name. There was nothing to link to, so
  // whichever profile ran first wrote its own — and the accounts drifted apart
  // in silence until one held the only real history.
  const out = engine(`
import json, os, tempfile
from broker import layout

class Provider:
    SHARED_GLOBS = ("*.sqlite",)
    SHARED = ()

with tempfile.TemporaryDirectory() as root:
    Provider.CANONICAL_HOME = os.path.join(root, "home")
    prof = os.path.join(root, "home-acct")
    os.makedirs(Provider.CANONICAL_HOME)
    os.makedirs(prof)

    # One the canonical home has never heard of, with a checkpoint beside it.
    open(os.path.join(prof, "state_5.sqlite"), "w").write("new")
    open(os.path.join(prof, "state_5.sqlite-wal"), "w").write("wal")
    # ...and one it already has: that one is a choice between two copies, and
    # stays a deliberate act rather than something a plain run decides.
    open(os.path.join(Provider.CANONICAL_HOME, "logs_2.sqlite"), "w").write("theirs")
    open(os.path.join(prof, "logs_2.sqlite"), "w").write("mine")

    moved = layout.promote(Provider, prof)
    link = os.path.join(prof, "state_5.sqlite")
    print(json.dumps({
        "moved": moved,
        "now_a_link": os.path.islink(link),
        # realpath both sides: on macOS the temp root is /var, a symlink to
        # /private/var, and only one of the two comes back resolved.
        "points_at_canonical": os.path.realpath(link) == os.path.realpath(
            os.path.join(Provider.CANONICAL_HOME, "state_5.sqlite")),
        "content_kept": open(link).read(),
        "wal_followed": os.path.exists(os.path.join(Provider.CANONICAL_HOME, "state_5.sqlite-wal")),
        "existing_untouched": open(os.path.join(prof, "logs_2.sqlite")).read(),
        "canonical_untouched": open(os.path.join(Provider.CANONICAL_HOME, "logs_2.sqlite")).read(),
    }))
`);
  const got = JSON.parse(out);
  assert.deepEqual(got.moved, ["state_5.sqlite"]);
  assert.equal(got.now_a_link, true, "the profile keeps reaching it, by link");
  assert.equal(got.points_at_canonical, true);
  assert.equal(got.content_kept, "new", "the data moves, it is not recreated empty");
  assert.equal(got.wal_followed, true, "a stranded -wal would lose a checkpoint");
  // Nothing is overwritten when both sides have a copy.
  assert.equal(got.existing_untouched, "mine");
  assert.equal(got.canonical_untouched, "theirs");
});

test("a box works on this window's databases, at their own path", async (t) => {
  const dir = await temp(t);
  const home = path.join(dir, ".codex");
  const project = path.join(dir, "project");
  const databases = path.join(dir, "codex-db", "w1");
  await fs.mkdir(home, { recursive: true });
  await fs.mkdir(project, { recursive: true });
  // The shared directory still has the old databases in it — a box must not
  // take those, and must not clone them either.
  await fs.writeFile(path.join(home, "state_5.sqlite"), "db");
  await fs.writeFile(path.join(home, "state_5.sqlite-wal"), "pages not yet folded in");
  await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({
    demo: { rw: [project] },
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes
from broker.providers import codex
box.boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
codex.CANONICAL_HOME = ${JSON.stringify(home)}
codex.BOX_HOME = (${JSON.stringify(home)},)
cmd = box.command(codex, "demo", boxes.profiles()["demo"], [],
                  {"CODEX_HOME": ${JSON.stringify(home)},
                   "CODEX_SQLITE_HOME": ${JSON.stringify(databases)}})
mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
inside = {}
for m in mounts:
    inside[m.split("target=")[1].split(",")[0]] = m.split("source=")[1].split(",")[0]
print(json.dumps({
    "databases": inside.get(${JSON.stringify(databases)}),
    "cloned": sorted(t for t, src in inside.items() if "/box/private/" in src),
}))
`, { BROKER_CONFIG_DIR: dir, HOME: dir });

  const got = JSON.parse(out);
  // Mounted as a DIRECTORY, at the same path inside as out: the harness is
  // pointed at it by its own variable, and a directory can be written in,
  // added to, and — when a database is damaged — have that file renamed out of
  // the way. A database mounted as a file cannot be renamed at all, and the
  // harness that tries refuses to start: "Resource busy".
  assert.equal(got.databases, databases, "this window's databases come in at their own path");
  assert.deepEqual(got.cloned, [], "and nothing is cloned — there is nothing shared left to clone");
});

test("state a box must not share can be keyed to the window", async (t) => {
  // A box's name is the same in every window, so a directory named after the
  // box is one directory for all of them. Measured here as thirteen boxes
  // writing into a single runner state, where one run's teardown deleted
  // another's job files mid-run.
  const out = engine(`
import json, os
from broker.box import paths

os.environ["HERDR_PANE_ID"] = "wM:p8"
first = paths.expand("~/.cache/box/{window}/state")
key_one = paths.window_key()

os.environ["HERDR_PANE_ID"] = "wM:pK"
second = paths.expand("~/.cache/box/{window}/state")

os.environ.pop("HERDR_PANE_ID", None)
print(json.dumps({
    "first": first,
    "second": second,
    "differ": first != second,
    "key_not_empty": bool(key_one),
    # A path without the placeholder is untouched: every other box keeps
    # sharing what it was already sharing.
    "plain": paths.expand("~/.cache/box/state"),
    "no_braces_left": "{window}" not in first,
}))
`);
  const got = JSON.parse(out);
  assert.equal(got.differ, true, "two windows must not land on one directory");
  assert.equal(got.key_not_empty, true, "an empty key would rebuild the shared path");
  assert.equal(got.no_braces_left, true);
  assert.ok(got.plain.endsWith("/.cache/box/state"), "paths without the placeholder are untouched");
});

test("a file the harness will not open through a link gets a second name instead", async (t) => {
  const dir = await temp(t);
  const canonical = path.join(dir, ".codex");
  const profile = path.join(dir, ".codex-other");
  await fs.mkdir(canonical, { recursive: true });
  // The profile directory exists before this runs: the broker makes it when it
  // writes the account's own credentials into it.
  await fs.mkdir(profile, { recursive: true });
  await fs.writeFile(path.join(canonical, ".credentials.json"), '{"ntk":"x"}');
  await fs.writeFile(path.join(canonical, "config.toml"), "");

  const out = engine(`
import json, os
from broker import layout
from broker.providers import codex
codex.CANONICAL_HOME = ${JSON.stringify(canonical)}
layout.prepare(codex, ${JSON.stringify(profile)})

creds = os.path.join(${JSON.stringify(profile)}, ".credentials.json")
conf = os.path.join(${JSON.stringify(profile)}, "config.toml")
print(json.dumps({
    "creds_is_symlink": os.path.islink(creds),
    "same_file": os.stat(creds).st_ino == os.stat(os.path.join(${JSON.stringify(canonical)}, ".credentials.json")).st_ino,
    "conf_is_symlink": os.path.islink(conf),
}))
`, { HOME: dir });

  const seen = JSON.parse(out);
  // Not a symlink: this harness refuses to open its token file through one and
  // reports "too many levels of symbolic links" for a single link.
  assert.equal(seen.creds_is_symlink, false);
  // But still ONE file — copies would drift, and OAuth rotates the refresh
  // token, so the profile that refreshed last would strand all the others.
  assert.equal(seen.same_file, true);
  // Everything else is shared the ordinary way.
  assert.equal(seen.conf_is_symlink, true);
});
