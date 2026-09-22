const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const root = path.join(__dirname, "..");

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-box-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  // realpath: on macOS the temp directory sits under /var, which is itself a
  // symlink to /private/var — and the code under test resolves symlinks.
  return fs.realpath(dir);
}

// The engine builds the command line; running python is how we see it.
function engine(code, env = {}) {
  return execFileSync("python3", ["-c", `import sys; sys.path.insert(0, 'lib/wrappers')\n${code}`],
    { cwd: root, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", ...env } });
}

test("--box is taken out of the arguments, and everything after -- is left alone", () => {
  const out = engine(`
from broker import box
from broker.box import boxes, mcp, run
import json
print(json.dumps([
  box.take_flag(["--box", "work", "-p", "hi"]),
  box.take_flag(["--box=work", "--resume", "abc"]),
  box.take_flag(["-p", "no box here"]),
  box.take_flag(["--box", "work", "--", "--box", "this is a prompt"]),
]))
`);
  assert.deepEqual(JSON.parse(out), [
    ["work", ["-p", "hi"]],
    ["work", ["--resume", "abc"]],
    [null, ["-p", "no box here"]],
    // A prompt that mentions --box is a prompt. Only the flag before -- is ours.
    ["work", ["--", "--box", "this is a prompt"]],
  ]);
});

test("the box carries the harness's own directory and the project, with the token only in the environment", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const reference = path.join(dir, "reference");
  await fs.mkdir(project);
  await fs.mkdir(reference);

  const out = engine(`
import json, os
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
cmd = box.command(claude, "demo",
                  {"rw": [${JSON.stringify(project)}], "ro": [${JSON.stringify(reference)}]},
                  ["-p", "hi"],
                  {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "BROKER_ACTIVE": "claude:sk"})
print(json.dumps(cmd))
`);
  const cmd = JSON.parse(out);
  const line = cmd.join(" ");
  const home = os.homedir();

  assert.equal(cmd[1], "run");
  assert.ok(line.includes(`--user ${process.getuid()}:${process.getgid()}`), "runs as you, so new files are yours");
  assert.ok(line.includes(`--tmpfs ${home}:uid=${process.getuid()}`), "$HOME must be writable inside");

  // Same path inside as outside — session history is keyed by it.
  assert.ok(line.includes(`source=${project},target=${project}`), "the project keeps its path");
  assert.ok(line.includes(`source=${reference},target=${reference},readonly`), "ro stays ro");
  assert.ok(line.includes(`source=${home}/.claude,target=${home}/.claude`), "settings, MCP and history come along");

  // The account's token rides in the environment and is never written to disk.
  assert.ok(line.includes("-e CLAUDE_CODE_OAUTH_TOKEN=fake-token"));
  // .credentials.json is NOT that token — it holds the logins for the MCP
  // servers, which are the same services whichever account is picked. Covering
  // it with a read-only empty file started every box logged out of all of them,
  // with nowhere to save a new login.
  assert.ok(!line.includes(`target=${home}/.claude/.credentials.json`),
    "MCP logins travel with the directory, and a login inside a box sticks");
  // Image, entry point, harness, then exactly what you typed — nothing rewritten.
  assert.deepEqual(cmd.slice(-5), ["broker-box", "broker-box-entry", "claude", "-p", "hi"]);
});

test("a file-credentials harness gets its per-account profile, not the shared directory's token", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const profile = path.join(dir, "codex-sk");
  await fs.mkdir(project);
  await fs.mkdir(profile);

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import codex
cmd = box.command(codex, "demo", {"rw": [${JSON.stringify(project)}]}, ["exec", "hi"],
                  {"CODEX_HOME": ${JSON.stringify(profile)}, "BROKER_ACTIVE": "codex:sk"})
print(json.dumps(cmd))
`);
  const line = JSON.parse(out).join(" ");
  assert.ok(line.includes(`source=${profile},target=${profile}`), "the account's profile comes in");
  assert.ok(line.includes(`-e CODEX_HOME=${profile}`), "and the harness is pointed at it");
  assert.ok(line.includes(`target=${os.homedir()}/.codex/auth.json,readonly`), "the shared directory's own token is covered");
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
from broker.box import boxes, mcp, run
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

test("only a box that runs containers gets a daemon, root and privileges", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const build = (extra) => JSON.parse(engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
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

test("a box gets only the ssh keys it names, and knows only the hosts it uses", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const keys = path.join(dir, "keys");
  await fs.mkdir(project);
  await fs.mkdir(keys);
  await fs.writeFile(path.join(keys, "box_key"), "not a real key\n", { mode: 0o600 });
  await fs.writeFile(path.join(keys, "personal_key"), "not a real key either\n", { mode: 0o600 });

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo", {
  "rw": ["${project}"],
  "ssh": {"hosts": {"10.0.0.5": "${keys}/box_key"}},
}, [], {})))
`, { HOME: dir });
  const line = JSON.parse(out).join(" ");

  assert.ok(line.includes(`source=${keys}/box_key`), "the named key comes in");
  // Not forbidden — absent. Nothing inside can use a key that was never mounted,
  // however it is asked to.
  assert.ok(!line.includes("personal_key"), "every other key stays out");
  assert.ok(line.includes(`target=${dir}/.ssh/config,readonly`));

  const conf = await fs.readFile(path.join(dir, ".config", "broker", "box", "ssh-config-demo"), "utf8");
  // The tools that need this call plain `ssh <host>` with no -i of their own.
  assert.match(conf, /Host 10\.0\.0\.5/);
  assert.match(conf, /IdentitiesOnly yes/);
});

test("a host can be a name that is not an address, without carrying your ssh config in", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const keys = path.join(dir, "keys");
  await fs.mkdir(project);
  await fs.mkdir(keys);
  await fs.writeFile(path.join(keys, "box_key"), "not a real key\n", { mode: 0o600 });

  engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = None
box.command(claude, "demo", {
  "rw": ["${project}"],
  "ssh": {"hosts": {
    "plain": "${keys}/box_key",
    "alias": {"key": "${keys}/box_key", "hostname": "100.64.0.7", "user": "someone", "port": 2222},
  }},
}, [], {})
`, { HOME: dir });

  const conf = await fs.readFile(path.join(dir, ".config", "broker", "box", "ssh-config-demo"), "utf8");
  // Your own ~/.ssh/config does not come along, so an alias that resolves on the
  // host would resolve to nothing in here unless the box spells it out.
  assert.match(conf, /Host alias\n  HostName 100\.64\.0\.7\n  User someone\n  Port 2222\n  IdentityFile/);
  // The short form still means exactly what it did.
  assert.match(conf, /Host plain\n  IdentityFile .*box_key\n  IdentitiesOnly yes/);
});

test("a box without an ssh section gets no ssh material at all", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo", {"rw": ["${project}"]}, [], {})))
`, { HOME: dir });
  assert.ok(!JSON.parse(out).join(" ").includes(".ssh"), "mounting ~/.ssh is never implicit");
});

test("a box can give a shared tool its own copy of a directory the host also has", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const own = path.join(dir, "own-state");
  await fs.mkdir(project);

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo", {"rw": [
  "${project}",
  {"source": "${own}", "target": "/shared/tool/.state"},
]}, [], {})))
`, { HOME: dir });
  const cmd = JSON.parse(out);
  const line = cmd.join(" ");

  // Two boxes writing into one state would mix their work; each gets its own,
  // mounted where the tool insists on looking.
  assert.ok(line.includes(`source=${own},target=/shared/tool/.state`));
  // Created on demand: its own directory cannot be expected to exist yet.
  assert.ok((await fs.stat(own)).isDirectory());
  // The working directory is the path as the BOX sees it, never the host's.
  assert.equal(cmd[cmd.indexOf("-w") + 1], project);
});

test("the working directory inside a box is the physical path, not the link you typed", async (t) => {
  const dir = await temp(t);
  const physical = path.join(dir, "elsewhere", "project");
  const link = path.join(dir, "Projects", "project");
  await fs.mkdir(path.join(physical, "sub"), { recursive: true });
  await fs.mkdir(path.dirname(link), { recursive: true });
  await fs.symlink(physical, link);

  const out = engine(`
import json, os
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = None
os.chdir("${link}/sub")
print(json.dumps(box.command(claude, "demo", {"rw": ["${link}"]}, [], {})))
`, { HOME: dir });
  const cmd = JSON.parse(out);

  // Tools that key work off the directory resolve symlinks first: starting from
  // the link made one dispatcher treat the project as a different repository
  // and fail the build on a package that was there all along.
  assert.equal(cmd[cmd.indexOf("-w") + 1], path.join(physical, "sub"));
});

test("paths in a box resolve against your real home, not a profile handed to a harness", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  // A file-credentials harness gets its account profile through $HOME. By the
  // time the box is built, "~" no longer means what it says — and ~/.gemini
  // resolved into the profile itself, so the real one was never mounted and
  // every symlink the profile makes back into it dangled.
  const out = engine(`
import json, os
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import agy
agy.MCP_CONFIG = None
os.environ["HOME"] = os.path.expanduser("~/.some-profile")
print(json.dumps(box.command(agy, "demo", {"rw": ["${project}"]}, [], {})))
`, { HOME: dir });
  const line = JSON.parse(out).join(" ");

  assert.ok(line.includes(`source=${dir}/.gemini,target=${dir}/.gemini`) || !line.includes(".gemini"),
    "the harness directory is looked for in the real home");
  assert.ok(!line.includes(".some-profile/.gemini"), "never inside the profile");
});

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
from broker.box import boxes, mcp, run
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

test("a box decides what flags the harness gets, and yours still win", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const run = (args, argv) => JSON.parse(engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo",
  {"rw": ["${project}"], "args": ${JSON.stringify(args)}}, ${JSON.stringify(argv)}, {})))
`, { HOME: dir }));

  // Per harness, because they spell the same idea differently.
  assert.deepEqual(run({ claude: ["--box-flag"] }, ["-p", "hi"]).slice(-4), ["claude", "--box-flag", "-p", "hi"]);
  assert.deepEqual(run({ codex: ["--other"] }, []).slice(-1), ["claude"], "another harness's flags are not ours");
  // A plain list is for every harness in the box.
  assert.deepEqual(run(["--everywhere"], []).slice(-2), ["claude", "--everywhere"]);
  // The box says it first, so the same flag typed by hand is the one that counts.
  assert.deepEqual(run({ claude: ["--mode"] }, ["--mode", "plan"]).slice(-3), ["claude", "--mode", "plan"],
    "a flag already typed is not added twice");
});

test("leaving a box says how to come back into it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const sessions = path.join(dir, ".claude", "projects", project.replace(/\//g, "-"));
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(sessions, { recursive: true });
  await fs.writeFile(path.join(sessions, "only.jsonl"), "{}\n");

  const out = engine(`
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"
print(box.run._resume_hint(claude, "demo", "${project}") or "NONE")
`, { HOME: dir });

  // The harness prints its own resume line, and that one reopens the session on
  // the HOST — a different world, which is not obvious until something behaves
  // oddly. This one names the box.
  assert.match(out, /claude --box demo --resume only/);

  // With a second session written in the same window there is no way to tell
  // which one was this box's, and the newest is a coin toss — on this machine
  // a losing one, since every window open on a project writes here. Point at
  // the id the harness itself just printed instead of naming a stranger's.
  await fs.writeFile(path.join(sessions, "another.jsonl"), "{}\n");
  const ambiguous = engine(`
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"
print(box.run._resume_hint(claude, "demo", "${project}") or "NONE")
`, { HOME: dir });
  assert.match(ambiguous, /claude --box demo --resume <the id printed above>/);
  assert.ok(!/only|another/.test(ambiguous), "no session is named when it cannot be known");
});

test("each harness is told to resume the way it spells it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project, { recursive: true });
  // codex keeps one pile per config directory, named by when the session
  // started, with the id at the end.
  const codexSessions = path.join(dir, "cfg", "sessions", "2026", "09", "20");
  await fs.mkdir(codexSessions, { recursive: true });
  await fs.writeFile(path.join(codexSessions,
    "rollout-2026-09-20T10-00-00-019efe7b-889a-72d3-8a7c-bfae7be3dacd.jsonl"), "{}\n");

  const out = engine(`
from broker import box
from broker.providers import codex
codex.SESSION_GLOB = "%(config)s/sessions/*/*/*/rollout-*.jsonl"
codex.SESSION_RESUME = "resume %s"
print(box.run._resume_hint(codex, "demo", "${project}", {"CODEX_HOME": "${path.join(dir, "cfg")}"}) or "NONE")
`, { HOME: dir });

  // Its own verb, and the id taken off the end of a timestamped name.
  assert.match(out, /codex --box demo resume 019efe7b-889a-72d3-8a7c-bfae7be3dacd/);
});

test("a session written before the box started is not mistaken for this one", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const sessions = path.join(dir, ".claude", "projects", project.split(path.sep).join("-"));
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(sessions, { recursive: true });
  await fs.writeFile(path.join(sessions, "earlier.jsonl"), "{}\n");

  // Harnesses that keep one pile for every project would otherwise offer the
  // newest file on the machine, which may belong to another window entirely.
  const out = engine(`
import time
from broker import box
from broker.providers import claude
claude.SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"
print(box.run._resume_hint(claude, "demo", "${project}", {}, time.time() + 60) or "NONE")
`, { HOME: dir });
  // The way back into the box is still worth saying; the id is not ours to
  // guess, and the harness printed its own one line above.
  assert.match(out, /claude --box demo --resume <the id printed above>/);
  assert.ok(!/earlier/.test(out), "a session from before the box started is not offered");
});


test("the session a box offers to resume is its own, not the newest on the machine", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const sessions = path.join(dir, ".claude", "projects", project.split(path.sep).join("-"));
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(sessions, { recursive: true });
  // Another window, open on the same project, writing its own session into the
  // one directory they share — and writing it last.
  await fs.writeFile(path.join(sessions, "aaaaaaaa-1111-2222-3333-444444444444.jsonl"), "{}\n");

  const out = engine(`
from broker import box
from broker.providers import claude
claude.SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"
claude.SESSION_ID_FLAG = ("--session-id", "%s")
claude.SESSION_PICKERS = ("--resume", "-r", "--continue", "-c", "--session-id")

pinned, argv = box.run._pin_session(claude, ["--dangerously-skip-permissions"])
print("FLAG", argv[0], argv[1] == pinned, argv[2])
print(box.run._resume_hint(claude, "demo", "${project}", {}, 0, None, pinned))
`, { HOME: dir });

  // The id is decided before the run, passed to the harness, and printed back
  // unchanged — the neighbour's newer file never enters into it.
  assert.match(out, /FLAG --session-id True --dangerously-skip-permissions/);
  const offered = out.match(/--resume ([0-9a-f-]{36})/);
  assert.ok(offered, out);
  assert.notStrictEqual(offered[1], "aaaaaaaa-1111-2222-3333-444444444444");
});

test("naming a session yourself leaves the command exactly as you typed it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project, { recursive: true });

  const out = engine(`
from broker import box
from broker.providers import claude
claude.SESSION_ID_FLAG = ("--session-id", "%s")
claude.SESSION_PICKERS = ("--resume", "-r", "--continue", "-c", "--session-id")

# Resuming by id: yours, and already known.
print("BYID", *box.run._pin_session(claude, ["--resume", "bbbbbbbb-1111-2222-3333-444444444444"]))
# A picker with nothing to pick from yet: the answer lives inside the harness.
print("PICKER", box.run._pin_session(claude, ["--continue"])[0])
# A harness that cannot be told an id keeps the old way of finding out.
claude.SESSION_ID_FLAG = None
print("UNTOLD", box.run._pin_session(claude, ["--print", "hi"]))
`, { HOME: dir });

  // Nothing added, nothing reordered: a session the person named is theirs.
  assert.match(out, /BYID bbbbbbbb-1111-2222-3333-444444444444 \['--resume', 'bbbbbbbb-1111-2222-3333-444444444444'\]/);
  assert.match(out, /PICKER None/);
  assert.match(out, /UNTOLD \(None, \['--print', 'hi'\]\)/);
});


test("a box can stand in for a command that must not run inside it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const out = engine(`
import json
from broker import box
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo", {
  "rw": ["${project}"],
  "stubs": {"~/bin/thing": "not in a box; use its MCP tools"},
}, [], {})))
`, { HOME: dir });

  // Deliberately absent is not the same as missing: an agent told to run a
  // command it cannot find searches the whole disk and then asks where it is.
  assert.match(JSON.parse(out).join(" "), /source=.*box\/stubs\/demo\/thing,target=.*bin\/thing,readonly/);
  const stub = await fs.readFile(path.join(dir, ".config", "broker", "box", "stubs", "demo", "thing"), "utf8");
  assert.match(stub, /not in a box; use its MCP tools/);
  assert.match(stub, /exit 127/, "it fails like a missing command, but says why");
});

test("the resume line names an account only when the session would be lost without it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const sessions = path.join(dir, "cfg", "sessions", "2026", "09", "20");
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(sessions, { recursive: true });
  // A profile that reaches the canonical pile through a link — the normal
  // arrangement once the profiles share one set of databases and sessions.
  await fs.mkdir(path.join(dir, "profile"), { recursive: true });
  await fs.symlink(path.join(dir, "cfg", "sessions"), path.join(dir, "profile", "sessions"));
  await fs.writeFile(path.join(sessions,
    "rollout-2026-09-20T10-00-00-019efe7b-889a-72d3-8a7c-bfae7be3dacd.jsonl"), "{}\n");

  const hint = (provider, account) => engine(`
from broker import box
from broker.providers import ${provider}
${provider}.SESSION_GLOB = "%(config)s/sessions/*/*/*/rollout-*.jsonl" if "${provider}" == "codex" else "%(home)s/.claude/projects/%(key)s/*.jsonl"
print(box.run._resume_hint(${provider}, "demo", "${project}", {"CODEX_HOME": "${path.join(dir, "cfg")}"}, 0, ${JSON.stringify(account)}) or "NONE")
`, { HOME: dir });

  // Sessions inside the profile: the broker moves to another account when one
  // runs out of room, and an id recorded under the first is then not found.
  assert.match(hint("codex", "sk"), /CODEX_ACCOUNT=sk codex --box demo resume /);

  // Sessions reached through a link instead. Naming an account here says
  // nothing that the line does not already say, and picking one is the
  // broker's job to begin with.
  const shared = engine(`
from broker import box
from broker.providers import codex
codex.SESSION_GLOB = "%(config)s/sessions/*/*/*/rollout-*.jsonl"
codex.CANONICAL_HOME = ${JSON.stringify(path.join(dir, "cfg"))}
print(box.run._resume_hint(codex, "demo", ${JSON.stringify(project)},
      {"CODEX_HOME": ${JSON.stringify(path.join(dir, "profile"))}}, 0, "sk") or "NONE")
`, { HOME: dir });
  assert.doesNotMatch(shared, /CODEX_ACCOUNT/);
  assert.match(shared, /codex --box demo resume /);
  // claude keeps its sessions outside any profile, so naming an account there
  // would only be noise.
  await fs.mkdir(path.join(dir, ".claude", "projects", project.split(path.sep).join("-")), { recursive: true });
  await fs.writeFile(path.join(dir, ".claude", "projects", project.split(path.sep).join("-"), "s.jsonl"), "{}\n");
  assert.doesNotMatch(hint("claude", "sk"), /CLAUDE_ACCOUNT/);
});

test("a box reaches shared directories through every profile, not just this run's", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  // Sessions are shared already: each profile's directory is a link into the
  // canonical home.
  const canonical = path.join(dir, ".tool");
  await fs.mkdir(path.join(canonical, "sessions"), { recursive: true });
  for (const account of ["one", "two"]) {
    await fs.mkdir(path.join(dir, `.tool-${account}`), { recursive: true });
    await fs.symlink(path.join(canonical, "sessions"), path.join(dir, `.tool-${account}`, "sessions"));
  }

  const out = engine(`
import json
from broker import box
from broker.providers import codex
codex.MCP_CONFIG = None
codex.CANONICAL_HOME = "${canonical}"
codex.BOX_SHARED = ("sessions",)
print(json.dumps(box.command(codex, "demo", {"rw": ["${project}"]}, [], {"CODEX_HOME": "${path.join(dir, ".tool-one")}"})))
`, { HOME: dir });
  const line = JSON.parse(out).join(" ");

  // A harness records the path it saw, through whichever profile was current.
  // Resuming under another account then fails on a file that is right there.
  assert.ok(line.includes(`target=${dir}/.tool-one/sessions`), "this run's profile");
  assert.ok(line.includes(`target=${dir}/.tool-two/sessions`), "and the one it used to be");
  // Only that directory: another account's credentials stay out.
  assert.ok(!line.includes(`${dir}/.tool-two/auth`), "nothing else of another account");
});

test("a box puts the terminal back, whatever killed it", () => {
  // A harness in a box switches the terminal to the alternate screen, asks for
  // mouse reports and turns on the kitty keyboard protocol. Killed outright it
  // undoes none of it, and the shell underneath then reads Enter as "27;3u".
  const out = engine(`
import ast, io, json, sys
from broker.box import run

wrote = io.StringIO()


class Tty(io.StringIO):
    def isatty(self):
        return True


# What the sequence actually turns off.
reset = run.TERMINAL_RESET
modes = {
    "alternate screen": "\\033[?1049l" in reset,
    "kitty keyboard": "\\033[<u" in reset,
    "cursor keys": "\\033[?1l" in reset,
    "bracketed paste": "\\033[?2004l" in reset,
    "mouse": "\\033[?1000l" in reset and "\\033[?1006l" in reset,
    "cursor shown": "\\033[?25h" in reset,
}

# On a terminal it is written; on a pipe it is not, or a redirected run would
# collect escape bytes in its output file.
tty, sys.stdout = sys.stdout, Tty()
run._restore_terminal(None)
on_tty = sys.stdout.getvalue()
sys.stdout = io.StringIO()
run._restore_terminal(None)
on_pipe = sys.stdout.getvalue()
sys.stdout = tty

# The restore has to sit in a finally, or an exception on the way out skips it.
tree = ast.parse(io.open("lib/wrappers/broker/box/run.py", encoding="utf-8").read())
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "exec_box")
guarded = any(
    any("_restore_terminal" == getattr(getattr(c, "func", None), "id", None)
        for c in ast.walk(ast.Module(body=node.finalbody, type_ignores=[])))
    for node in ast.walk(fn) if isinstance(node, ast.Try) and node.finalbody
)

print(json.dumps({
    "modes": modes,
    "on_tty": on_tty == reset,
    "on_pipe": on_pipe,
    "guarded": guarded,
    "signals": sorted(
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in ("SIGTERM", "SIGHUP")
    ),
}))
`);
  const got = JSON.parse(out);
  for (const [mode, present] of Object.entries(got.modes)) {
    assert.equal(present, true, `the reset must turn off ${mode}`);
  }
  assert.equal(got.on_tty, true, "a terminal gets the full sequence");
  assert.equal(got.on_pipe, "", "a pipe gets nothing — no escapes in a log file");
  assert.equal(got.guarded, true, "exec_box must restore the terminal in a finally");
  // SIGKILL cannot be caught; 'broker box repair' is the way back from that one.
  assert.deepEqual(got.signals, ["SIGHUP", "SIGTERM"]);
});

test("a database the harness invents in a profile becomes everyone's", () => {
  // codex added state_5.sqlite, and later thread_history_1.sqlite, at a moment
  // when the canonical home had no such name. There was nothing to link to, so
  // whichever profile ran first wrote its own — and the accounts drifted apart
  // in silence until one held the only real history.
  const out = engine(`
import json, os, tempfile
from broker import profile


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

    moved = profile.promote(Provider, prof)
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

test("a box gets its own sqlite journals, not only its own databases", async (t) => {
  const dir = await temp(t);
  const home = path.join(dir, ".codex");
  const project = path.join(dir, "project");
  await fs.mkdir(home, { recursive: true });
  await fs.mkdir(project, { recursive: true });
  // A database with a journal beside it, and one without — sqlite writes the
  // journal on first open, so the box needs its own either way.
  await fs.writeFile(path.join(home, "state_5.sqlite"), "db");
  await fs.writeFile(path.join(home, "state_5.sqlite-wal"), "pages not yet folded in");
  await fs.writeFile(path.join(home, "logs_2.sqlite"), "db");
  await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({
    demo: { rw: [project] },
  }));

  const out = engine(`
import json, os
from broker import box
from broker.box import boxes
from broker.providers import codex
box.boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
codex.CANONICAL_HOME = ${JSON.stringify(home)}
codex.BOX_HOME = (${JSON.stringify(home)},)
cmd = box.command(codex, "demo", boxes.profiles()["demo"], [], {"CODEX_HOME": ${JSON.stringify(home)}})
mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
inside = {}
for m in mounts:
    src = m.split("source=")[1].split(",")[0]
    inside[m.split("target=")[1]] = src
print(json.dumps({
    "targets": sorted(t for t in inside if ".sqlite" in t),
    "all_private": all("/box/private/" in src for t, src in inside.items() if ".sqlite" in t),
    "wal_content": open(inside[os.path.join(${JSON.stringify(home)}, "state_5.sqlite-wal")]).read(),
}))
`, { BROKER_CONFIG_DIR: dir, HOME: dir });

  const got = JSON.parse(out);
  // sqlite keeps -wal/-shm BESIDE the database. Cloning the database alone
  // left those coming from the directory mount — shared with the host — so the
  // box wrote its pages into its own copy and its journal into everyone's.
  for (const suffix of ["", "-wal", "-shm"]) {
    assert.ok(got.targets.includes(path.join(home, `state_5.sqlite${suffix}`)),
      `state_5.sqlite${suffix} must be mounted privately`);
  }
  // Even where the host has no journal yet: sqlite would otherwise create one
  // in the shared directory the moment it opens the database.
  assert.ok(got.targets.includes(path.join(home, "logs_2.sqlite-wal")));
  assert.equal(got.all_private, true, "every sqlite path comes from the box's own copy");
  assert.equal(got.wal_content, "pages not yet folded in",
    "an existing journal is cloned, not replaced — its pages are the newest ones");
});

test("a session written in a box joins the history out here, in order", async (t) => {
  const dir = await temp(t);
  const log = path.join(dir, "sync.log");
  const out = engine(`
import json, os, time
from broker.box import run


class Provider:
    NAME = "demo"
    BIN = "demo"
    # Two steps: a thread started in a box lives in the box's copy of the
    # database, and one call is not enough to take it into the history here.
    BOX_SYNC = (("archive", "%(session)s"), ("unarchive", "%(session)s"))


run.SYNC_LOG = ${JSON.stringify(log)}
run.real_bin = lambda p: "/bin/echo"
import broker.run
broker.run.real_bin = lambda p: "/bin/echo"
run._sync_back(Provider, "SID")
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

test("image pins follow the machine forward, never backward", async (t) => {
  const dir = await temp(t);
  const context = path.join(dir, "box");
  await fs.mkdir(context, { recursive: true });
  await fs.writeFile(path.join(context, "Dockerfile"), [
    "FROM node:24-trixie-slim",
    "ARG CLAUDE_VERSION=2.1.267",
    "ARG GH_VERSION=2.100.0",
    "",
  ].join("\n"));

  const box = require("../lib/box");
  const original = box.installedVersion;
  t.after(() => { box.installedVersion = original; });

  // This machine is behind on one tool and ahead on the other — the situation
  // that lowered six pins for everybody when a colleague ran `upgrade`.
  box.installedVersion = (spec) => ({ CLAUDE_VERSION: "2.1.251", GH_VERSION: "2.101.0" })[spec.arg];

  // laterVersion is what decides, and it has to compare numbers as numbers:
  // 1.116.0 is newer than 1.107.0, and 2.101.0 newer than 2.100.0.
  assert.equal(box.laterVersion("1.116.0", "1.107.0"), "1.116.0");
  assert.equal(box.laterVersion("2.101.0", "2.100.0"), "2.101.0");
  assert.equal(box.laterVersion("1.4.2", "1.3.14"), "1.4.2");
  assert.equal(box.laterVersion("v5.5.1", "v5.4.0"), "v5.5.1");
  // An older pin genuinely loses to a newer install.
  assert.equal(box.laterVersion("2.1.251", "2.1.267"), "2.1.267");
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
