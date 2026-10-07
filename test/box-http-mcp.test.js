const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const root = path.join(__dirname, "..");

async function fixture(t, kind, body) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-http-mcp-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const home = path.join(dir, "home");
  const project = path.join(dir, "project");
  await fs.mkdir(home);
  await fs.mkdir(project);
  const source = path.join(home, kind === "toml" ? "config.toml" : "config.json");
  await fs.writeFile(source, body);
  return { dir, home, project, source };
}

function command(f, kind, wholeDirectory, accountHome) {
  const code = `
import json, os, sys
from types import SimpleNamespace
sys.path.insert(0, "lib/wrappers")
from broker import config
from broker.box import extras, run
config.CONFIG_DIR = ${JSON.stringify(path.join(f.dir, "broker"))}
extras._passwd_file = lambda *args: None
provider = SimpleNamespace(NAME="probe", BIN="echo", CREDENTIALS=None,
    BOX_HOME=(${JSON.stringify(wholeDirectory ? f.home : f.source)},),
    HOME_ENV="CODEX_HOME", CANONICAL_HOME=${JSON.stringify(f.home)},
    MCP_CONFIG=(${JSON.stringify(f.source)}, ${JSON.stringify(kind)},
                ${JSON.stringify(kind === "toml" ? "mcp_servers" : "mcpServers")}))
print(json.dumps(run.command(provider, "demo", {"rw": [${JSON.stringify(f.project)}]}, [],
    ${JSON.stringify(accountHome ? { CODEX_HOME: accountHome } : {})})))
`;
  return JSON.parse(execFileSync("python3", ["-c", code], {
    cwd: root, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  }));
}

function configMount(cmd, target) {
  const mounts = cmd.filter((arg) => arg.startsWith("type=bind,") && arg.includes(`target=${target}`));
  assert.equal(mounts.length, 1);
  const source = mounts[0].match(/(?:^|,)source=([^,]+)/)[1];
  return source;
}

test("a box reaches the host HTTP MCP without changing its JSON config", async (t) => {
  const original = JSON.stringify({ mcpServers: {
    agntbus: { type: "http", url: "http://127.0.0.1:18496/mcp" },
    public: { type: "http", url: "https://example.test/mcp" },
  }, other: "http://127.0.0.1:9999/keep" });
  const f = await fixture(t, "json", original);
  const cmd = command(f, "json", false);
  const source = configMount(cmd, f.source);
  const staged = JSON.parse(await fs.readFile(source, "utf8"));
  assert.equal(staged.mcpServers.agntbus.url, "http://host.docker.internal:18496/mcp");
  assert.equal(staged.mcpServers.public.url, "https://example.test/mcp");
  assert.equal(staged.other, "http://127.0.0.1:9999/keep");
  assert.equal(await fs.readFile(f.source, "utf8"), original);
});

test("Agy serverUrl reaches the host HTTP MCP", async (t) => {
  const original = JSON.stringify({ mcpServers: {
    agntbus: { serverUrl: "http://127.0.0.1:18496/mcp" },
  } });
  const f = await fixture(t, "json", original);
  const cmd = command(f, "json", true);
  const source = configMount(cmd, await fs.realpath(f.source));
  const staged = JSON.parse(await fs.readFile(source, "utf8"));
  assert.equal(staged.mcpServers.agntbus.serverUrl,
    "http://host.docker.internal:18496/mcp");
  assert.equal(await fs.readFile(f.source, "utf8"), original);
});

test("a nested TOML config gets its own file above the mounted home", async (t) => {
  const original = '[mcp_servers.agntbus]\nurl = "http://localhost:18496/mcp"\n';
  const f = await fixture(t, "toml", original);
  const cmd = command(f, "toml", true);
  const source = configMount(cmd, await fs.realpath(f.source));
  assert.match(await fs.readFile(source, "utf8"), /host\.docker\.internal:18496/);
  assert.equal(await fs.readFile(f.source, "utf8"), original);
});

test("an account profile uses its own MCP config in the box", async (t) => {
  const f = await fixture(t, "toml", '[mcp_servers.agntbus]\nurl = "https://example.test/mcp"\n');
  const account = path.join(f.dir, "account");
  await fs.mkdir(account);
  const profileConfig = path.join(account, "config.toml");
  const original = '[mcp_servers.agntbus]\nurl = "http://127.0.0.1:18496/mcp"\n';
  await fs.writeFile(profileConfig, original);
  const cmd = command(f, "toml", true, account);
  const source = configMount(cmd, await fs.realpath(profileConfig));
  assert.match(await fs.readFile(source, "utf8"), /host\.docker\.internal:18496/);
  assert.equal(await fs.readFile(profileConfig, "utf8"), original);
  assert.equal(await fs.readFile(f.source, "utf8"),
    '[mcp_servers.agntbus]\nurl = "https://example.test/mcp"\n');
});
