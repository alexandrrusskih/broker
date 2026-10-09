// The bridge itself: stdio both ways, the secret it demands, how long it lives,
// and whose identity it belongs to.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { root, temp, engine } = require("./helpers");

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
    const sock = net.connect(state.port, state.host || "127.0.0.1");
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

test("a client that is still connected keeps the bridge alive", async (t) => {
  const dir = await temp(t);
  const env = { ...process.env, HOME: dir, PYTHONDONTWRITEBYTECODE: "1", BROKER_MCP_IDLE_SECONDS: "1" };
  for (const name of Object.keys(env)) if (name.startsWith("HERDR_")) delete env[name];
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

  // A harness opens its MCP server once and holds that connection for the whole
  // session. The idle timeout used to be measured between CONNECTIONS, so a
  // conversation that outlasted it lost its server mid-sentence.
  const held = net.connect(state.port, state.host || "127.0.0.1");
  t.after(() => held.destroy());
  await new Promise((resolve, reject) => {
    held.on("connect", () => { held.write(`${state.token}\nfirst\n`); resolve(); });
    held.on("error", reject);
  });
  const answered = await new Promise((resolve) => {
    let seen = "";
    held.on("data", (d) => { seen += d; if (seen.includes("\n")) resolve(seen.trim()); });
    setTimeout(() => resolve(seen.trim()), 5000);
  });
  assert.equal(answered, "first");

  // Well past the idle timeout, with nobody new connecting.
  await new Promise((r) => setTimeout(r, 3000));
  assert.equal(bridge.exitCode, null, "the listener must still be running");
  const laterAnswer = await new Promise((resolve) => {
    let seen = "";
    held.on("data", (d) => { seen += d; if (seen.includes("\n")) resolve(seen.trim()); });
    held.write("second\n");
    setTimeout(() => resolve(seen.trim()), 5000);
  });
  assert.equal(laterAnswer, "second", "the held connection still reaches the server");
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

// The host and the box must agree: a run that owns its MCP file sees none of the
// user's servers in either place. The box needs no separate switch for it —
// mcp_servers() already reads the path through HOME_ENV, so the profile the run
// uses decides what gets bridged.

test("box env overrides launch env for the harness and host-side MCP bridge", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", env_vars: ["Q"] } }
  }));
  const inspect = (launchQ) => JSON.parse(engine(`
import json
from broker import box, mcpbridge
from broker.box import mcp
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
profile = {"rw": ["${project}"], "env": {"Q": "2"},
           "mcp": {"probe": {"identity_env": ["Q"]}}}
mcp._start_bridge = lambda *a: {"port": 41234, "token": "fake", "command": ["x"]}
cmd = box.command(claude, "demo", profile, [], {})
values = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "-e" and cmd[i + 1].startswith("Q=")]
server = mcp.mcp_servers(claude)["probe"]
env = mcp._bridge_env("probe", server, profile, ["${project}"])
print(json.dumps([values, env["Q"], mcpbridge.identity_key(env=env, extra=["Q"])]))
`, { HOME: dir, Q: launchQ }));
  const first = inspect("1");
  const second = inspect("9");
  assert.deepEqual(first[0], ["Q=1", "Q=1", "Q=2"]);
  assert.equal(first[1], "2");
  assert.deepEqual(second[0], ["Q=9", "Q=9", "Q=2"]);
  assert.equal(second[1], "2");
  assert.equal(second[2], first[2], "the effective box env also identifies the MCP listener");
});

// Crew builds its own container and needs the same bridge a box gets: the
// server stays on this machine and only its stdio crosses. One supported call
// writes the connector; the listener's port, its secret and the template stay
// inside broker. Reuse must tell callers apart — a server answering about code
// must answer about the CALLER's code — so the identity folds in the env and
// the allowed root, not just the command.
test("a connector for a host MCP server is written once per caller identity", async (t) => {
  const dir = await temp(t);
  // HOME, not a patched attribute: the listener is a child process and works
  // its own config directory out from the environment, so the two have to agree
  // or the parent waits five seconds for a listener it will never find.
  const out = engine(`
import json, os, re, sys
sys.path.insert(0, "lib/wrappers")
from broker import box, config

def port_of(file):
    # The template spells it HOST, PORT, TOKEN = 'host', 41234, 'secret'
    return int(re.search(r"TOKEN = .*?, (\\d+),", open(file).read()).group(1))

first = box.connect_mcp("probe", ["cat"], os.path.join(${JSON.stringify(dir)}, "a"),
                        env={"PROBE_ROOT": ${JSON.stringify(dir)}}, roots=[${JSON.stringify(dir)}])
again = box.connect_mcp("probe", ["cat"], os.path.join(${JSON.stringify(dir)}, "b"),
                        env={"PROBE_ROOT": ${JSON.stringify(dir)}}, roots=[${JSON.stringify(dir)}])
other = box.connect_mcp("probe", ["cat"], os.path.join(${JSON.stringify(dir)}, "c"),
                        env={"PROBE_ROOT": "/somewhere/else"}, roots=["/somewhere/else"])

body = open(first).read()
print(json.dumps({
    "mode": oct(os.stat(first).st_mode & 0o777),
    "reaches_host": "host.docker.internal" in body,
    "keeps_secret_out_of_argv": "TOKEN" in body,
    "same_identity_same_listener": port_of(first) == port_of(again),
    "other_identity_own_listener": port_of(first) != port_of(other),
}))

# Listeners outlive this process on purpose; this test does not leave them.
import glob, signal
for state in glob.glob(os.path.join(config.CONFIG_DIR, "box", "mcp", "*")):
    try:
        os.kill(json.load(open(state))["pid"], signal.SIGTERM)
    except Exception:
        pass
`, { HOME: dir, BROKER_REAL_HOME: dir });
  const got = JSON.parse(out);
  assert.equal(got.mode, "0o700", "it carries the connection secret");
  assert.equal(got.reaches_host, true);
  assert.equal(got.keeps_secret_out_of_argv, true, "the secret is in the file, not a command line");
  assert.equal(got.same_identity_same_listener, true, "one listener per caller, reused");
  assert.equal(got.other_identity_own_listener, true,
    "a different allowed root is a different caller and gets its own listener");
});
