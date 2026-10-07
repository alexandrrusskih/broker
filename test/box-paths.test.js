// Where a box looks: its ssh material, its own copies, and the paths it resolves.
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
from broker.box import boxes, mcp, run, sync
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
from broker.box import boxes, mcp, run, sync
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
from broker.box import boxes, mcp, run, sync
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
from broker.box import boxes, mcp, run, sync
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
from broker.box import boxes, mcp, run, sync
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
from broker.box import boxes, mcp, run, sync
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
