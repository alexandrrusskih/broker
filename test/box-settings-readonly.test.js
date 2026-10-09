const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

test("host settings stay read-only in every box", async (t) => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "broker-settings-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const project = path.join(home, "project");
  const files = [
    ".codex/config.toml", ".codex/hooks.json", ".claude/settings.json",
    ".claude.json", ".gemini/settings.json", ".gemini/config/mcp_config.json",
    ".gemini/config/hooks.json", ".config/opencode/opencode.jsonc",
  ];
  await fs.mkdir(project);
  for (const name of files) {
    const file = path.join(home, name);
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, name.endsWith(".toml") ? "" : "{}");
  }
  const account = path.join(home, ".codex-sk");
  await fs.mkdir(account);
  for (const name of ["config.toml", "hooks.json"]) {
    await fs.symlink(path.join(home, ".codex", name), path.join(account, name));
  }
  const code = `
import json, sys
sys.path.insert(0, "lib/wrappers")
from broker.box import extras, run
from broker.providers import codex, claude, agy, opencode
extras._passwd_file = lambda *args: None
result = {}
for provider in (codex, claude, agy, opencode):
    env = {"CODEX_HOME": ${JSON.stringify(account)}} if provider.NAME == "codex" else {}
    cmd = run.command(provider, "demo", {"rw": [${JSON.stringify(project)}], "mcp": False}, [], env)
    result[provider.NAME] = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--mount"]
print(json.dumps(result))
`;
  const mounts = JSON.parse(execFileSync("python3", ["-c", code], {
    cwd: path.join(__dirname, ".."), encoding: "utf8",
    env: { ...process.env, HOME: home, BROKER_REAL_HOME: home, PYTHONDONTWRITEBYTECODE: "1" },
  }));
  const required = {
    codex: [".codex/config.toml", ".codex/hooks.json"],
    claude: [".claude/settings.json"],
    agy: [".gemini/settings.json", ".gemini/config/mcp_config.json",
          ".gemini/config/hooks.json"],
    opencode: [".config/opencode/opencode.jsonc"],
  };
  for (const [provider, names] of Object.entries(required)) {
    for (const name of names) {
      const target = path.join(home, name);
      assert.ok(mounts[provider].some((m) => m.includes(`target=${target},readonly`)),
        `${provider}: ${name} must be read-only`);
    }
  }
  const profileMount = mounts.codex.find((m) => m.includes(`target=${account}`));
  assert.ok(profileMount);
  assert.ok(!profileMount.includes(`source=${account},`), "Codex profile is isolated");
});

// The settings file being read-only is not enough: the commands inside it name
// scripts, and those sit in the harness's own directory, which a box gets
// writable. A box that rewrites one has the HOST harness run it on the next
// session event, outside any container.
test("a hook script the host settings name is read-only too", async (t) => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "broker-hooks-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const project = path.join(home, "project");
  const inside = path.join(home, ".claude", "hooks", "state.sh");
  const outside = path.join(home, "bin", "elsewhere.sh");
  for (const file of [inside, outside]) {
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  }
  await fs.mkdir(project);
  // A name with a space in it has to be quoted to be a name at all, so the
  // quote is what delimits it. A verifier found the first pass stopped at the
  // space and left such a script writable.
  const spaced = path.join(home, ".claude", "hooks", "with space.sh");
  await fs.writeFile(spaced, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  await fs.writeFile(path.join(home, ".claude", "settings.json"), JSON.stringify({
    hooks: {
      SessionStart: [{ hooks: [
        { type: "command", command: `bash '${inside}' session` },
        { type: "command", command: `bash "${spaced}" session` },
        { type: "command", command: outside },
        { type: "command", command: "rtk hook claude" },
      ] }],
    },
  }));

  const code = `
import json, sys
sys.path.insert(0, "lib/wrappers")
from broker.box import extras, run
from broker.providers import claude
extras._passwd_file = lambda *args: None
cmd = run.command(claude, "demo", {"rw": [${JSON.stringify(project)}], "mcp": False}, [], {})
print(json.dumps([cmd[i + 1] for i, part in enumerate(cmd) if part == "--mount"]))
`;
  const mounts = JSON.parse(execFileSync("python3", ["-c", code], {
    cwd: path.join(__dirname, ".."), encoding: "utf8",
    env: { ...process.env, HOME: home, BROKER_REAL_HOME: home, PYTHONDONTWRITEBYTECODE: "1" },
  }));

  assert.ok(mounts.some((m) => m.includes(`target=${inside},readonly`)),
    "a script named inside the harness's own directory must be read-only");
  assert.ok(mounts.some((m) => m.includes(`target=${spaced},readonly`)),
    "a quoted name with a space in it must be read-only too");
  assert.ok(!mounts.some((m) => m.includes(`target=${outside}`)),
    "a script outside it is the box's own business, and mounting it could collide");

  // Docker refuses a container with two mounts at one target, so a settings
  // file that names a path already mounted must not add a second one.
  const targets = mounts.map((m) => m.split("target=")[1].split(",")[0]);
  assert.equal(new Set(targets).size, targets.length, "no mount target twice");
});

// The same escape, for the harness whose hooks live in their own file rather
// than in its settings. agy keeps them in ~/.gemini/config/hooks.json, which
// was writable from a box while the script it names sat beside it — so a box
// could rewrite either and wait for the next session out here.
test("agy's hooks file and the script it names are read-only too", async (t) => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "broker-agy-hooks-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const project = path.join(home, "project");
  const hooks = path.join(home, ".gemini", "config", "hooks.json");
  const script = path.join(home, ".gemini", "config", "hooks", "state.sh");
  await fs.mkdir(project);
  await fs.mkdir(path.dirname(script), { recursive: true });
  await fs.writeFile(script, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  await fs.writeFile(path.join(home, ".gemini", "settings.json"), "{}");
  await fs.writeFile(hooks, JSON.stringify({
    herdr: { PreInvocation: [{ type: "command", command: `bash '${script}' session` }] },
  }));

  const code = `
import json, sys
sys.path.insert(0, "lib/wrappers")
from broker.box import extras, run
from broker.providers import agy
extras._passwd_file = lambda *args: None
cmd = run.command(agy, "demo", {"rw": [${JSON.stringify(project)}], "mcp": False}, [], {})
print(json.dumps([cmd[i + 1] for i, part in enumerate(cmd) if part == "--mount"]))
`;
  const mounts = JSON.parse(execFileSync("python3", ["-c", code], {
    cwd: path.join(__dirname, ".."), encoding: "utf8",
    env: { ...process.env, HOME: home, BROKER_REAL_HOME: home, PYTHONDONTWRITEBYTECODE: "1" },
  }));

  assert.ok(mounts.some((m) => m.includes(`target=${hooks},readonly`)),
    "the file that names what the host runs must be read-only");
  assert.ok(mounts.some((m) => m.includes(`target=${script},readonly`)),
    "and so must the script it names");
  const targets = mounts.map((m) => m.split("target=")[1].split(",")[0]);
  assert.equal(new Set(targets).size, targets.length, "no mount target twice");
});
