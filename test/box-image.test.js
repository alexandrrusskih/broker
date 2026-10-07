// The image a box runs, the versions pinned into it, and what is said when the
// box or the daemon is not there.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { root, temp, engine } = require("./helpers");

test("a box that needs more than the base brings its own Dockerfile", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(path.join(project, ".box"), { recursive: true });
  await fs.writeFile(path.join(project, ".box", "Dockerfile"), "FROM broker-box\nUSER root\n");
  await fs.mkdir(path.join(dir, ".config", "broker"), { recursive: true });
  await fs.writeFile(path.join(dir, ".config", "broker", "boxes.json"), JSON.stringify({
    plain: { rw: [project] },
    extended: { rw: [project], dockerfile: path.join(project, ".box", "Dockerfile") },
  }));

  // Which image each box runs — the engine decides this on its own, from the
  // same rule the builder tags with.
  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
claude.MCP_CONFIG = None
box.boxes.PATH = "${path.join(dir, ".config", "broker", "boxes.json")}"
p = box.profiles()
print(json.dumps([
  box.command(claude, "plain", p["plain"], [], {})[-3],
  box.command(claude, "extended", p["extended"], [], {})[-3],
]))
`, { HOME: dir });
  assert.deepEqual(JSON.parse(out), ["broker-box", "broker-box-extended"]);

  // The builder agrees. Run in its own process: the module resolves the profile
  // path once, when it is first loaded.
  const fakeBin = path.join(dir, "bin");
  await fs.mkdir(fakeBin);
  await fs.writeFile(path.join(fakeBin, "docker"), '#!/bin/sh\necho "$@" >> "$HOME/docker-calls"\n', { mode: 0o755 });
  const built = JSON.parse(execFileSync(process.execPath,
    ["-e", "process.stdout.write(JSON.stringify(require('./lib/box').buildBoxes({})))"],
    { cwd: root, encoding: "utf8", env: { ...process.env, HOME: dir, PATH: `${fakeBin}:${process.env.PATH}` } }));

  assert.equal(built.length, 1, "only boxes that name a Dockerfile are built");
  assert.equal(built[0].tag, "broker-box-extended");
  // Built from its own directory, so the Dockerfile can COPY what sits beside it.
  const call = await fs.readFile(path.join(dir, "docker-calls"), "utf8");
  assert.match(call.trim(), /build -t broker-box-extended -f .*\.box\/Dockerfile .*\.box$/);
});

test("a version pin follows the machine into a box's own Dockerfile", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(path.join(project, ".box"), { recursive: true });
  const own = path.join(project, ".box", "Dockerfile");
  await fs.writeFile(own, "FROM broker-box\nARG GH_VERSION=1.0.0\n");
  await fs.mkdir(path.join(dir, ".config", "broker"), { recursive: true });
  await fs.writeFile(path.join(dir, ".config", "broker", "boxes.json"), JSON.stringify({
    extended: { rw: [project], dockerfile: own },
  }));

  // A tool that lives in one box rather than the base still has to move with
  // the machine — otherwise the box that needs it most is the one left behind.
  const fakeBin = path.join(dir, "bin");
  await fs.mkdir(fakeBin);
  await fs.writeFile(path.join(fakeBin, "gh"), '#!/bin/sh\necho "gh version 2.101.0"\n', { mode: 0o755 });
  const changed = JSON.parse(execFileSync(process.execPath,
    ["-e", "process.stdout.write(JSON.stringify(require('./lib/box').syncPins({})))"],
    { cwd: root, encoding: "utf8", env: { ...process.env, HOME: dir, PATH: `${fakeBin}:${process.env.PATH}` } }));

  assert.ok(changed.some((c) => c.arg === "GH_VERSION" && c.to === "2.101.0"), JSON.stringify(changed));
  assert.match(await fs.readFile(own, "utf8"), /ARG GH_VERSION=2\.101\.0/);
});

test("image pins follow the machine forward, never backward", async (t) => {
  const dir = await temp(t);
  const dockerfile = path.join(dir, "Dockerfile");
  await fs.writeFile(dockerfile, [
    "FROM node:24-trixie-slim",
    "ARG CLAUDE_VERSION=2.1.267",
    "ARG GH_VERSION=2.100.0",
    "",
  ].join("\n"));

  const pins = require("../lib/box-pins");
  // This machine is behind on one tool and ahead on the other — the situation
  // that lowered six pins for everybody when a colleague ran `upgrade`.
  const installed = (_spec, arg) =>
    ({ CLAUDE_VERSION: "2.1.251", GH_VERSION: "2.101.0" })[arg];

  const changed = [];
  const skipped = [];
  pins.syncPinsIn(dockerfile, { installed }, changed, skipped);
  const text = await fs.readFile(dockerfile, "utf8");

  // The tool this machine is ahead on moves; the one it is behind on stays.
  assert.deepEqual(changed, [{ arg: "GH_VERSION", from: "2.100.0", to: "2.101.0" }]);
  assert.deepEqual(skipped, [{ arg: "CLAUDE_VERSION", pinned: "2.1.267", installed: "2.1.251" }]);
  assert.match(text, /ARG GH_VERSION=2\.101\.0/);
  assert.match(text, /ARG CLAUDE_VERSION=2\.1\.267/);

  // ...unless the downgrade is asked for by name.
  const down = [];
  pins.syncPinsIn(dockerfile, { installed, allowDowngrade: true }, down, []);
  assert.deepEqual(down, [{ arg: "CLAUDE_VERSION", from: "2.1.267", to: "2.1.251" }]);

  // laterVersion is what decides, and it has to compare numbers as numbers:
  // 1.116.0 is newer than 1.107.0, and 2.101.0 newer than 2.100.0.
  assert.equal(pins.laterVersion("1.116.0", "1.107.0"), "1.116.0");
  assert.equal(pins.laterVersion("2.101.0", "2.100.0"), "2.101.0");
  assert.equal(pins.laterVersion("1.4.2", "1.3.14"), "1.4.2");
  assert.equal(pins.laterVersion("v5.5.1", "v5.4.0"), "v5.5.1");
  // An older pin genuinely loses to a newer install.
  assert.equal(pins.laterVersion("2.1.251", "2.1.267"), "2.1.267");
});

test("a stopped container daemon says so, instead of offering a line to resume", async (t) => {
  const dir = await temp(t);
  const file = path.join(dir, "boxes.json");
  await fs.writeFile(file, `{ "work": { "rw": ["~"], "runtime": "false" } }`);
  const out = engine(`
import contextlib, io
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import codex
box.boxes.PATH = ${JSON.stringify(file)}
said, code = io.StringIO(), None
with contextlib.redirect_stderr(said):
    try:
        box.exec_box(codex, "work", ["resume", "019efe7b-889a-72d3-8a7c-bfae7be3dacd"], {})
    except SystemExit as exc:
        code = exc.code
print("exit", code)
print(said.getvalue())
`, { HOME: dir });

  assert.match(out, /exit 1/, "a box that cannot start is a failure, not a success");
  assert.match(out, /false daemon is not running/, "it names the thing that is down");
  assert.doesNotMatch(out, /Resume it in this box/,
    "nothing ran in there, so there is nothing to resume");
  assert.doesNotMatch(out, /Cannot connect|image cache/,
    "the preflight happens before anything tries the daemon and warns about it");
});

test("an undefined box names what is defined instead of failing blankly", async (t) => {
  const dir = await temp(t);
  const file = path.join(dir, "boxes.json");
  await fs.writeFile(file, `{
    // comments are allowed: this file is edited by hand
    "work": { "rw": ["~"] }, /* and so are these */
    "other": { "rw": ["~"] }
  }`);
  const out = engine(`
from broker import box
from broker.box import boxes, mcp, run, sync
box.boxes.PATH = ${JSON.stringify(file)}
print(sorted(box.profiles()))
try:
    box.exec_box(None, "missing", [], {})
except SystemExit as exc:
    print("exit", exc.code)
`);
  assert.match(out, /\['other', 'work'\]/, "comments do not stop it parsing");
  assert.match(out, /exit 1/);
});

// A stopped Docker used to produce three messages and no answer: the registry
// cache warned, `docker run` printed the daemon's own "Cannot connect" line, and
// then the box offered "Resume it in this box with: <the command that just
// failed>". The runtime here is `false`, which exists and answers nothing — the
// same shape as a daemon that is not running.

test("only a box that runs containers gets a daemon, root and privileges", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const build = (extra) => JSON.parse(engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo", {"rw": ["${project}"]${extra}}, [], {})))
`, { HOME: dir })).join(" ");

  const plain = build("");
  assert.ok(plain.includes(`--user ${process.getuid()}:${process.getgid()}`), "no daemon, no root");
  assert.ok(!plain.includes("--privileged"));
  assert.ok(!plain.includes("BROKER_BOX_DOCKER"));

  const withDocker = build(', "docker": True');
  // The daemon needs root, so the container starts as root and the entry point
  // drops to your uid — otherwise files written into the project come back
  // owned by root.
  assert.ok(!withDocker.includes("--user "), "root at start, dropped by the entry point");
  assert.ok(withDocker.includes("--privileged"));
  assert.ok(withDocker.includes(`BROKER_BOX_UID=${process.getuid()}`));
  // Its own layer store, so two boxes never share images — and per window, since
  // a daemon owns that directory exclusively and a second box of the same name
  // would find it taken and start with no daemon at all.
  assert.match(withDocker, /type=volume,source=broker-box-docker-demo[^,]*,target=\/var\/lib\/docker/);
});
