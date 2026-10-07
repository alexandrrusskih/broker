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
    ".config/opencode/opencode.jsonc",
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
    agy: [".gemini/settings.json", ".gemini/config/mcp_config.json"],
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
