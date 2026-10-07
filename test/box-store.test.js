// Folding a session written inside a box back into the history out here.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { temp, engine } = require("./helpers");

test("a session written in a box joins the history out here, in order", async (t) => {
  const dir = await temp(t);
  const log = path.join(dir, "sync.log");
  const out = engine(`
import json, os, time
from broker.box import sync

class Provider:
    NAME = "demo"
    BIN = "demo"
    # Two steps: a thread started in a box lives in the box's copy of the
    # database, and one call is not enough to take it into the history here.
    BOX_SYNC = (("archive", "%(session)s"), ("unarchive", "%(session)s"))

sync.SYNC_LOG = ${JSON.stringify(log)}
import broker.run
broker.run.real_bin = lambda p: "/bin/echo"
sync._sync_back(Provider, "SID")
# Started and left to run: nothing here waits for it.
for _ in range(50):
    time.sleep(0.1)
    if os.path.exists(${JSON.stringify(log)}) and "unarchive" in open(${JSON.stringify(log)}).read():
        break
print(json.dumps({"log": open(${JSON.stringify(log)}).read()}))
`);
  const { log: text } = JSON.parse(out);
  // Both steps ran, and in the order the provider listed them.
  assert.ok(text.includes("archive SID"), "the first step runs");
  assert.ok(text.includes("unarchive SID"), "and so does the second");
  assert.ok(text.indexOf("archive SID") < text.indexOf("unarchive SID"),
    "order matters: the second undoes the first");
});

test("a harness whose history is one database folds its session back by asking itself", async (t) => {
  const dir = await temp(t);
  const store = path.join(dir, ".config", "broker", "box", "private", "demo", "opencode");
  await fs.mkdir(store, { recursive: true });
  await fs.writeFile(path.join(store, "opencode.db"), "x");

  const out = engine(`
from broker import box
from broker.box import store
from broker.providers import opencode

# What the fold would run, without running it.
private = store._private_store(opencode, "demo")
print(private)
print(opencode.BOX_SYNC_SHELL % {"session": "ses_TEST", "bin": "/bin/oc",
                                 "store_parent": "'" + private.rsplit("/", 1)[0] + "'"})
print(store._private_store(opencode, None))
`, { HOME: dir });

  const [found, script, missing] = out.trim().split("\n");
  assert.match(found, /box\/private\/demo\/opencode$/);
  // Read from the box's OWN copy, write through the harness's own import —
  // never by copying the database, which holds every session there has ever
  // been and would overwrite whatever happened outside meanwhile.
  assert.match(script, /XDG_DATA_HOME='.*box\/private\/demo' \/bin\/oc export ses_TEST/, script);
  assert.match(script, /\/bin\/oc import "\$f"/, script);
  assert.ok(!/cp |rsync|install -m/.test(script), "the database itself is never copied");
  // Outside a box there is no such copy, and nothing to fold.
  assert.equal(missing, "None");
});

test("folding a session back and copying the databases never overlap", async (t) => {
  const dir = await temp(t);
  const log = path.join(dir, "sync.log");
  const out = engine(`
import json, os, time
from broker.box import sync

class Provider:
    NAME = "demo"
    BIN = "demo"
    BOX_SYNC = (("archive", "%(session)s"), ("unarchive", "%(session)s"))

sync.SYNC_LOG = ${JSON.stringify(log)}
import broker.run
broker.run.real_bin = lambda p: "/bin/echo"

# Hold the lock the copying takes, then ask for the fold. It must wait: between
# archive and unarchive the session is archived, and a box copying the database
# in that instant carries that into a container which then cannot reopen its
# own work. That is the race that switched this off in the first place.
with sync._StoreLock(Provider):
    sync._sync_back(Provider, "SID")
    time.sleep(1.5)
    held = open(${JSON.stringify(log)}).read() if os.path.exists(${JSON.stringify(log)}) else ""

for _ in range(60):
    time.sleep(0.1)
    after = open(${JSON.stringify(log)}).read() if os.path.exists(${JSON.stringify(log)}) else ""
    if "unarchive SID" in after:
        break
print(json.dumps({"held": held, "after": after}))
`, { HOME: dir });

  const { held, after } = JSON.parse(out);
  // The log opens with the command it is about to run, so what proves a step
  // RAN is its own output on a line of its own — /bin/echo stands in for the
  // harness here.
  const ran = (text, step) => new RegExp(`^${step} SID$`, "m").test(text);
  assert.ok(!ran(held, "archive"),
    "nothing is folded back while the databases are being copied");
  assert.ok(ran(after, "archive") && ran(after, "unarchive"),
    "and once the copying is done, both steps run");
  assert.ok(after.indexOf("\narchive SID") < after.indexOf("\nunarchive SID"),
    "in the order the provider listed them");
});

test("a lock that is not shared is not a lock", async (t) => {
  const dir = await temp(t);
  const canonical = path.join(dir, ".codex");
  const profile = path.join(dir, ".codex-second");
  await fs.mkdir(path.join(canonical, "mcp-oauth-locks"), { recursive: true });
  await fs.writeFile(path.join(canonical, "mcp-oauth-locks", "file-store.lock"), "");
  await fs.writeFile(path.join(canonical, ".credentials.json"), '{"ntk":"x"}');
  // The profile already has one of its own — which is how it ends up in life:
  // the harness made it before anyone thought about sharing.
  await fs.mkdir(path.join(profile, "mcp-oauth-locks"), { recursive: true });

  const out = engine(`
import json, os
from broker import layout
from broker.providers import codex
codex.CANONICAL_HOME = ${JSON.stringify(canonical)}
layout.prepare(codex, ${JSON.stringify(profile)})
locks = os.path.join(${JSON.stringify(profile)}, "mcp-oauth-locks")
print(json.dumps({
    "is_link": os.path.islink(locks),
    "same": os.path.realpath(locks) == os.path.realpath(os.path.join(${JSON.stringify(canonical)}, "mcp-oauth-locks")),
}))
`, { HOME: dir });

  const seen = JSON.parse(out);
  // Several accounts run side by side and share one token file. With a lock
  // each, two of them refresh at once: one wins, the other is left holding a
  // refresh token the server has just revoked.
  assert.equal(seen.is_link, true, "the profile's own lock directory is replaced");
  assert.equal(seen.same, true, "and points at the one everybody else uses");
});
