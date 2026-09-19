const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const net = require("node:net");
const path = require("node:path");
const { execFileSync, spawn } = require("node:child_process");
const root = path.join(__dirname, "..");

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-mcp-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  return dir;
}

function engine(code, env = {}) {
  return execFileSync("python3", ["-c", `import sys; sys.path.insert(0, 'lib/wrappers')\n${code}`],
    { cwd: root, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", ...env } });
}

test("only command-started MCP servers are bridged; http ones are left alone", async (t) => {
  const dir = await temp(t);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: {
      local: { command: "/opt/tool/mcp", args: ["serve"], env: { TOOL_ROOT: "/somewhere" } },
      remote: { type: "http", url: "https://example.test/mcp" }
    }
  }));
  const out = engine(`
import json
from broker import box
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
print(json.dumps(box.mcp_servers(claude)))
`);
  assert.deepEqual(JSON.parse(out), {
    // A server reached over the network needs nothing from us.
    local: { command: ["/opt/tool/mcp", "serve"], env: { TOOL_ROOT: "/somewhere" } }
  });
});

test("the bridge carries stdio both ways and refuses a connection without the secret", async (t) => {
  const dir = await temp(t);
  const env = { HOME: dir, PYTHONDONTWRITEBYTECODE: "1" };
  // `cat` stands in for an MCP server: whatever goes in comes back out.
  const bridge = spawn("python3", ["-m", "broker.mcpbridge", "serve", "probe", "--", "cat"],
    { cwd: path.join(root, "lib", "wrappers"), env: { ...process.env, ...env }, stdio: "ignore" });
  t.after(() => bridge.kill());

  const statePath = path.join(dir, ".config", "broker", "box", "mcp", "probe.json");
  let state = null;
  for (let i = 0; i < 100 && !state; i++) {
    try { state = JSON.parse(await fs.readFile(statePath, "utf8")); }
    catch (_e) { await new Promise((r) => setTimeout(r, 50)); }
  }
  assert.ok(state, "the listener should publish its port");
  assert.equal((await fs.stat(statePath)).mode & 0o777, 0o600, "the secret is not world-readable");

  const talk = (secret, line) => new Promise((resolve, reject) => {
    const sock = net.connect(state.port, "127.0.0.1");
    let seen = "";
    sock.on("connect", () => sock.write(`${secret}\n${line}\n`));
    sock.on("data", (d) => { seen += d; if (seen.includes("\n")) { sock.end(); resolve(seen.trim()); } });
    sock.on("close", () => resolve(seen.trim()));
    sock.on("error", (e) => (e.code === "ECONNRESET" ? resolve("") : reject(e)));
    setTimeout(() => { sock.destroy(); resolve(seen.trim()); }, 5000);
  });

  assert.equal(await talk(state.token, "hello from the box"), "hello from the box");
  // A loopback port is reachable by everything on the machine, so the secret is
  // what stands between them and a spawned server.
  assert.equal(await talk("wrong-secret", "hello"), "", "no secret, no server, no answer");
  assert.equal(await talk(state.token, "still alive"), "still alive", "a refused client does not take the bridge down");
});

test("the shim stands in for the server's own command, at its own path", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", args: ["serve"] } }
  }));

  const out = engine(`
import json
from broker import box
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
box._start_bridge = lambda *a: {"port": 41234, "token": "fake-secret", "command": ["x"]}
print(json.dumps(box.command(claude, "demo", {"rw": ["${project}"]}, [], {})))
`, { HOME: dir });

  const cmd = JSON.parse(out);
  const line = cmd.join(" ");
  // The harness config already points at /opt/tool/mcp — so that is where the
  // stand-in goes, and nothing has to be rewritten.
  assert.match(line, /source=.*box\/shims\/claude\/probe,target=\/opt\/tool\/mcp,readonly/);
  assert.ok(line.includes("--add-host host.docker.internal:host-gateway"));

  const shim = await fs.readFile(path.join(dir, ".config", "broker", "box", "shims", "claude", "probe"), "utf8");
  assert.match(shim, /HOST, PORT, TOKEN = 'host\.docker\.internal', 41234, 'fake-secret'/);
  assert.match(shim, /read1/, "read() on a pipe blocks for a full buffer and would deadlock the bridge");
  assert.equal((await fs.stat(path.join(dir, ".config", "broker", "box", "shims", "claude", "probe"))).mode & 0o777, 0o700);
});

test("a box can turn bridging off, and can say what a server should see", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", env: { CBM_ALLOWED_ROOT: "/everything" } } }
  }));

  const off = engine(`
import json
from broker import box
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
print(json.dumps(box.command(claude, "demo", {"rw": ["${project}"], "mcp": False}, [], {})))
`, { HOME: dir });
  assert.ok(!off.includes("/opt/tool/mcp"), "'mcp': false means no bridges at all");

  // A server that answers questions about code must answer about THIS box's code.
  const env = engine(`
import json
from broker import box
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
servers = box.mcp_servers(claude)
print(json.dumps([
  box._bridge_env("probe", servers["probe"], {}, ["${project}"])["CBM_ALLOWED_ROOT"],
  box._bridge_env("probe", servers["probe"], {"mcp": {"probe": {"env": {"CBM_ALLOWED_ROOT": "/explicit"}}}}, ["${project}"])["CBM_ALLOWED_ROOT"],
]))
`, { HOME: dir });
  assert.deepEqual(JSON.parse(env), [project, "/explicit"]);
});
