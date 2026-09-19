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

test("the box carries the harness's own directory, the project, and no credentials file", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const reference = path.join(dir, "reference");
  await fs.mkdir(project);
  await fs.mkdir(reference);

  const out = engine(`
import json, os
from broker import box
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

  // The token rides in the environment; a copy on disk inside the box is a copy
  // that can leave it.
  assert.ok(line.includes(`target=${home}/.claude/.credentials.json,readonly`), "credentials are covered by an empty file");
  assert.ok(line.includes("-e CLAUDE_CODE_OAUTH_TOKEN=fake-token"));
  // Image, then the harness, then exactly what you typed — nothing rewritten.
  assert.deepEqual(cmd.slice(-4), ["broker-box", "claude", "-p", "hi"]);
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
box.PATH = ${JSON.stringify(file)}
print(sorted(box.profiles()))
try:
    box.exec_box(None, "missing", [], {})
except SystemExit as exc:
    print("exit", exc.code)
`);
  assert.match(out, /\['other', 'work'\]/, "comments do not stop it parsing");
  assert.match(out, /exit 1/);
});
