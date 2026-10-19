// Docker creates a missing mount point and every parent above it as root. Under
// a box's home that leaves a directory the box itself cannot write to, and the
// error names the child: "EACCES: permission denied, mkdir". So the parents are
// claimed first, derived from the mounts rather than named one at a time.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { temp, engine } = require("./helpers");

// The command line for a box whose profile is `profile`, with HOME set to `dir`.
function command(dir, profile) {
  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
print(json.dumps(box.command(claude, "demo", json.loads(${JSON.stringify(JSON.stringify(profile))}),
                             ["-p", "hi"], {"CLAUDE_CODE_OAUTH_TOKEN": "t"})))
`, { HOME: dir, BROKER_REAL_HOME: dir });
  return JSON.parse(out);
}

// Every mount point the command names, and how it was mounted.
function targets(cmd) {
  const found = [];
  cmd.forEach((part, i) => {
    if (cmd[i - 1] === "--mount") found.push(["bind", part.match(/target=([^,]+)/)[1]]);
    if (cmd[i - 1] === "--tmpfs") found.push(["tmpfs", part.split(":")[0]]);
  });
  return found;
}

test("a mount deep under home has its parents claimed, so the box can still write beside it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  // The case this was found by: six boxes of twelve on finik had ~/.cache owned
  // by root:root, because the codex session database is mounted below it. A
  // push gate that wanted ~/.cache/finik/pre-push-gates got EACCES, and the
  // cause was two directories above the one in the message.
  const deep = path.join(dir, ".cache", "broker-box", "codex-db", "41718f0d");
  const cmd = command(dir, {
    rw: [project, { source: path.join(dir, "store"), target: deep }]
  });
  const mounts = new Map(targets(cmd).map(([kind, target]) => [target, kind]));

  assert.equal(mounts.get(deep), "bind", "the database is still mounted where it was asked for");
  for (const parent of [path.join(dir, ".cache"),
                        path.join(dir, ".cache", "broker-box"),
                        path.join(dir, ".cache", "broker-box", "codex-db")]) {
    assert.equal(mounts.get(parent), "tmpfs",
      `${parent} belongs to the box, not to root — otherwise nothing below it can be created`);
  }
  // Owned by the user who runs inside, which is the whole point of claiming it.
  const claimed = cmd.find((part) => part.startsWith(path.join(dir, ".cache") + ":"));
  assert.match(claimed || "", new RegExp(`uid=${process.getuid()},gid=${process.getgid()}`),
    "a parent claimed as root's would be no better than one Docker made");
});

test("a directory inside a mounted project is left alone, because the host's own content is there", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(path.join(project, "sub", "real"), { recursive: true });

  // A tmpfs here would hide what the host really has in it — an empty
  // directory over a project's own files is worse than a root-owned one.
  const inside = path.join(project, "sub", "deep");
  const cmd = command(dir, { rw: [project, { source: path.join(dir, "store"), target: inside }] });
  const mounts = new Map(targets(cmd).map(([kind, target]) => [target, kind]));

  assert.equal(mounts.get(inside), "bind");
  assert.equal(mounts.get(path.join(project, "sub")), undefined,
    "the project is mounted, so this directory already exists and is the host's");
});

test("no mount point is named twice, however the parents are derived", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  // ~/.config is claimed by hand above, and ~/.bun/install is a parent twice
  // over: of the cache below it and of its own child. Docker refuses a
  // container that names one mount point twice.
  const cmd = command(dir, {
    rw: [project,
         { source: path.join(dir, "bun"), target: path.join(dir, ".bun", "install", "cache") },
         { source: path.join(dir, "glab"), target: path.join(dir, ".config", "glab", "config.yml") }]
  });

  const seen = targets(cmd).map(([, target]) => target);
  assert.equal(new Set(seen).size, seen.length,
    `one mount point each: ${seen.filter((p, i) => seen.indexOf(p) !== i).join(", ")}`);
  const mounts = new Map(targets(cmd).map(([kind, target]) => [target, kind]));
  assert.equal(mounts.get(path.join(dir, ".bun")), "tmpfs", "a bun cache still gets its parents");
  assert.equal(mounts.get(path.join(dir, ".bun", "install")), "tmpfs");
});
