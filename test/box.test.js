// What a box is: the arguments it takes out, the home and project it carries,
// and the credentials it does not.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { root, temp, engine } = require("./helpers");

test("--box is taken out of the arguments, and everything after -- is left alone", () => {
  const out = engine(`
from broker import box
from broker.box import boxes, mcp, run, sync
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
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
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
  await fs.writeFile(path.join(profile, "auth.json"), "profile-token");

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import codex
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(codex, "demo", {"rw": [${JSON.stringify(project)}]}, ["exec", "hi"],
                  {"CODEX_HOME": ${JSON.stringify(profile)}, "BROKER_ACTIVE": "codex:sk"})
print(json.dumps(cmd))
`);
  const line = JSON.parse(out).join(" ");
  const mount = JSON.parse(out).find((part) => part.includes(`target=${profile}`));
  assert.ok(mount, "the account's profile comes in");
  const source = mount.match(/source=([^,]+)/)[1];
  assert.notEqual(source, profile, "the box cannot replace a host profile symlink");
  assert.equal(await fs.readFile(path.join(source, "auth.json"), "utf8"), "profile-token");
  assert.ok(line.includes(`-e CODEX_HOME=${profile}`), "and the harness is pointed at it");
  assert.ok(line.includes(`target=${os.homedir()}/.codex/auth.json,readonly`), "the shared directory's own token is covered");
});

test("two hosts may share one key without the box refusing to start", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const key = path.join(dir, "id_shared");
  await fs.mkdir(project);
  await fs.writeFile(key, "private key");
  await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({
    demo: {
      rw: [project],
      ssh: { hosts: { "github.com": key, "git.example.com": key } },
    },
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes
from broker.providers import claude
box.boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
cmd = box.command(claude, "demo", boxes.profiles()["demo"], [], {})
print(json.dumps([m for m in cmd if ${JSON.stringify(key)} in m]))
`, { BROKER_CONFIG_DIR: dir, HOME: dir });

  // Mounted once, however many hosts name it. Twice and docker refuses the
  // whole run — "Duplicate mount point" — so the box does not start at all.
  assert.equal(JSON.parse(out).length, 1, "one key, one mount");
});

test("a harness the broker holds no credentials for still gets a box", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(path.join(dir, ".local", "share", "opencode"), { recursive: true });
  await fs.writeFile(path.join(dir, ".local", "share", "opencode", "opencode.db"), "x");
  await fs.mkdir(project, { recursive: true });

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import opencode
opencode.MCP_CONFIG = None
print(json.dumps(box.command(opencode, "demo", {"rw": ["${project}"]}, ["run", "hi"], {})))
`, { HOME: dir });

  const line = JSON.parse(out).join(" ");
  // Its database is one file for every session it has ever had, and the
  // journal lives beside it: shared across the boundary it tears, which is how
  // a day of another harness's history was lost. So the box gets a clone.
  assert.ok(!/source=.*\.local\/share\/opencode\/opencode\.db,/.test(line.replace(/box\/private[^,]*/g, "")),
    "the real database must not be mounted");
  assert.match(line, /box\/private\/demo\/opencode\/opencode\.db/, line);
  // Mounting a file deep under $HOME has the container create its parents as
  // root, and the harness — which runs as you — then cannot write beside its
  // own database.
  assert.match(line, new RegExp(`--tmpfs ${path.join(dir, ".local")}:uid=`), line);
});

test("the MCP logins of the machine are not the box's to empty", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const out = engine(`
import json
from broker import box
from broker.providers import agy
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(agy, "demo", {"rw": [${JSON.stringify(project)}]}, ["hi"], {})
print(json.dumps(cmd))
`);
  const line = JSON.parse(out).join(" ");
  const home = os.homedir();
  const tokens = `${home}/.gemini/antigravity-cli/mcp_oauth_tokens.json`;

  // This harness rewrites that file whole, keeping only what its own session
  // logged in — so a box, which knows nothing, writes an empty object over the
  // logins of this machine. It gets its own instead.
  assert.ok(line.includes(`target=${tokens}`), "the box's view of the tokens is covered");
  assert.ok(!line.includes(`source=${tokens}`), "and it is not the machine's file underneath");
  // Not read-only: the harness would fail on a write it expects to succeed,
  // instead of carrying on without the servers that need a login.
  assert.ok(!line.includes(`target=${tokens},readonly`), "a box may write its own");
});

// Nothing inside a box could tell it was in one: the paths match the host
// exactly, which is the point. A hook that must report the chat it is in to a
// terminal manager outside the container has to know to do that.
test("a box says that it is a box, and which one", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  const out = engine(`
import json
from broker import box
from broker.providers import claude
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(claude, "work", {"rw": [${JSON.stringify(project)}], "mcp": False}, [], {})
print(json.dumps([cmd[i + 1] for i, part in enumerate(cmd) if part == "-e"]))
`);
  const env = JSON.parse(out);
  assert.ok(env.includes("BROKER_BOX=work"), "the box says its own name");
  // The bus hooks run from the harness settings this box mounts, on the host
  // and in here alike, and their box branch reads this one.
  assert.ok(env.includes("AGNTBUS_BOX=1"), "the bus hooks are told they are boxed");
});
