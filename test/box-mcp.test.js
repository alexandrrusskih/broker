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
  // realpath: on macOS the temp directory sits under /var, which is itself a
  // symlink to /private/var — and the code under test resolves symlinks.
  return fs.realpath(dir);
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
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
print(json.dumps(box.mcp_servers(claude)))
`);
  assert.deepEqual(JSON.parse(out), {
    // A server reached over the network needs nothing from us.
    local: { command: ["/opt/tool/mcp", "serve"], env: { TOOL_ROOT: "/somewhere" }, inherit: [] }
  });
});

test("the bridge carries stdio both ways and refuses a connection without the secret", async (t) => {
  const dir = await temp(t);
  // No Herdr variables: the listener is then keyed by nothing and lands on the
  // plain name. Identity keying has a test of its own.
  const env = { ...process.env, HOME: dir, PYTHONDONTWRITEBYTECODE: "1" };
  for (const name of Object.keys(env)) if (name.startsWith("HERDR_")) delete env[name];
  // `cat` stands in for an MCP server: whatever goes in comes back out.
  const bridge = spawn("python3", ["-m", "broker.mcpbridge", "serve", "probe", "--", "cat"],
    { cwd: path.join(root, "lib", "wrappers"), env, stdio: "ignore" });
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

test("a bridge belongs to the identity that raised it", async (t) => {
  const dir = await temp(t);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp" } }
  }));
  // agentbus takes its bus identity from the Herdr pane it was started in, and
  // a listener outlives the shell that raised it. Reusing one across panes would
  // post to the bus as the wrong agent, in the wrong workspace.
  const key = (env) => engine(`
from broker import mcpbridge
print(mcpbridge.identity_key(${JSON.stringify(env)}))
`, { HOME: dir }).trim();

  const here = key({ HERDR_PANE_ID: "pane-1", HERDR_WORKSPACE_ID: "misc" });
  const otherPane = key({ HERDR_PANE_ID: "pane-2", HERDR_WORKSPACE_ID: "misc" });
  const otherWs = key({ HERDR_PANE_ID: "pane-1", HERDR_WORKSPACE_ID: "other-workspace" });
  assert.notEqual(here, otherPane, "another pane is another agent");
  assert.notEqual(here, otherWs, "another workspace is another group on the bus");
  assert.equal(here, key({ HERDR_PANE_ID: "pane-1", HERDR_WORKSPACE_ID: "misc" }), "the same pane reuses its bridge");
  // A machine with no panes at all keeps one bridge per server, as before.
  assert.equal(key({}), "");
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
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
box.mcp._start_bridge = lambda *a: {"port": 41234, "token": "fake-secret", "command": ["x"]}
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
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
print(json.dumps(box.command(claude, "demo", {"rw": ["${project}"], "mcp": False}, [], {})))
`, { HOME: dir });
  assert.ok(!off.includes("/opt/tool/mcp"), "'mcp': false means no bridges at all");

  // A server that answers questions about code must answer about THIS box's code.
  const env = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
servers = box.mcp_servers(claude)
print(json.dumps([
  box.mcp._bridge_env("probe", servers["probe"], {}, ["${project}"])["CBM_ALLOWED_ROOT"],
  box.mcp._bridge_env("probe", servers["probe"], {"mcp": {"probe": {"env": {"CBM_ALLOWED_ROOT": "/explicit"}}}}, ["${project}"])["CBM_ALLOWED_ROOT"],
]))
`, { HOME: dir });
  assert.deepEqual(JSON.parse(env), [project, "/explicit"]);
});

test("an explicit path in a box resolves symlinks too, so it cannot key a second database", async (t) => {
  const dir = await temp(t);
  const physical = path.join(dir, "elsewhere", "project");
  const link = path.join(dir, "Projects", "project");
  await fs.mkdir(physical, { recursive: true });
  await fs.mkdir(path.dirname(link), { recursive: true });
  await fs.symlink(physical, link);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", env: { CBM_ALLOWED_ROOT: "/everything" } } }
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
servers = box.mcp_servers(claude)
profile = {"mcp": {"probe": {"env": {
    "CBM_ALLOWED_ROOT": "${link}",
    "CBM_CACHE_DIR": "${dir}/cache/not-created-yet",
    "CBM_LABEL": "just a string"}}}}
env = box.mcp._bridge_env("probe", servers["probe"], profile, [])
print(json.dumps([env["CBM_ALLOWED_ROOT"], env["CBM_CACHE_DIR"], env["CBM_LABEL"]]))
`, { HOME: dir });

  const [root, cache, label] = JSON.parse(out);
  assert.equal(root, physical, "writing the symlinked spelling by hand must not start a second database");
  assert.equal(cache, path.join(dir, "cache", "not-created-yet"), "a path that does not exist yet is left as written");
  assert.equal(label, "just a string", "values that are not paths are untouched");
});

test("a symlinked project comes in under both names, and keeps its existing index", async (t) => {
  const dir = await temp(t);
  const physical = path.join(dir, "elsewhere", "project");
  const link = path.join(dir, "Projects", "project");
  await fs.mkdir(physical, { recursive: true });
  await fs.mkdir(path.dirname(link), { recursive: true });
  await fs.symlink(physical, link);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", env: { CBM_ALLOWED_ROOT: "/everything" } } }
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
box.mcp._start_bridge = lambda *a: None
cmd = box.command(claude, "demo", {"rw": ["${link}"]}, [], {})
servers = box.mcp_servers(claude)
print(json.dumps({
  "cmd": cmd,
  "root": box.mcp._bridge_env("probe", servers["probe"], {}, ["${link}"])["CBM_ALLOWED_ROOT"],
}))
`, { HOME: dir });
  const { cmd, root } = JSON.parse(out);
  const line = cmd.join(" ");

  // A host-side server resolves symlinks and answers with the physical path.
  // Without the second mount the harness inside cannot open a single file it names.
  assert.ok(line.includes(`source=${link},target=${link}`), "the name you typed");
  assert.ok(line.includes(`source=${physical},target=${physical}`), "and the path it really is");
  // The database is keyed by the path given, so the symlinked spelling would
  // start a second one and reindex the project from scratch.
  assert.equal(root, physical);
});

test("MCP is read from the profile the run actually uses, not the canonical home", async (t) => {
  const dir = await temp(t);
  const canonical = path.join(dir, ".codex");
  const profile = path.join(dir, ".codex-sk");
  await fs.mkdir(canonical);
  await fs.mkdir(profile);
  // The same server, spelled differently in each — which is what actually
  // happened: one through /Volumes, the other through ~/Projects.
  await fs.writeFile(path.join(canonical, "config.toml"),
    '[mcp_servers.probe]\ncommand = "/canonical/path/mcp"\n');
  await fs.writeFile(path.join(profile, "config.toml"),
    '[mcp_servers.probe]\ncommand = "/profile/path/mcp"\n');

  const read = (env) => JSON.parse(engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import codex
codex.MCP_CONFIG = ("${canonical}/config.toml", "toml", "mcp_servers")
codex.CANONICAL_HOME = ${JSON.stringify(canonical)}
print(json.dumps(box.mcp_servers(codex, ${JSON.stringify(env)})["probe"]["command"]))
`, { HOME: dir }));

  // An account's profile carries its own copy, and the two drift. Reading the
  // canonical one mounts the stand-in where the harness never looks, and the
  // server is reported missing.
  assert.deepEqual(read({ CODEX_HOME: profile }), ["/profile/path/mcp"]);
  assert.deepEqual(read({}), ["/canonical/path/mcp"], "with no profile in play, nothing changes");
});

test("variables a server is declared to inherit are carried into the box", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  await fs.writeFile(path.join(dir, "config.toml"),
    '[mcp_servers.probe]\ncommand = "/opt/tool/mcp"\nenv_vars = ["PROBE_ACTOR", "PROBE_ABSENT"]\n');

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import codex
codex.MCP_CONFIG = ("${dir}/config.toml", "toml", "mcp_servers")
box.mcp._start_bridge = lambda *a: {"port": 1, "token": "x", "command": ["y"]}
print(json.dumps(box.command(codex, "demo", {"rw": ["${project}"]}, [], {})))
`, { HOME: dir, PROBE_ACTOR: "reader" });

  const line = JSON.parse(out).join(" ");
  // codex declares which variables a server expects to inherit rather than
  // spelling out their values; inside a box nothing is inherited.
  assert.ok(line.includes("PROBE_ACTOR=reader"));
  assert.ok(!line.includes("PROBE_ABSENT"), "a variable that is not set is not invented");
});
