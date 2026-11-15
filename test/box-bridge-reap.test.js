// A bridge that ends must take its spawned servers with it. The per-session
// cleanup is correct but runs in a DAEMON thread, so when the listener process
// returns the interpreter kills that thread before its finally — and every
// server it spawned is re-parented to launchd and kept. Measured on one
// machine: 92 index servers, 30 of them with ppid 1 and no bridge left.
const test = require("node:test");
const assert = require("node:assert/strict");
const net = require("node:net");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");
const { temp, engine, root } = require("./helpers");

function alive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

async function until(what, tries = 100, pause = 100) {
  for (let i = 0; i < tries; i++) {
    const answer = what();
    if (answer) return answer;
    await new Promise((done) => setTimeout(done, pause));
  }
  return null;
}

function childrenOf(pid) {
  try {
    return execFileSync("pgrep", ["-P", String(pid)], { encoding: "utf8" })
      .split("\n").map((s) => s.trim()).filter(Boolean).map(Number);
  } catch { return []; }
}

test("close() ends the servers still registered", async (t) => {
  const out = engine(`
import subprocess, time
from broker.mcpserve import _Live
live = _Live()
kids = [subprocess.Popen(["sleep", "300"]) for _ in range(2)]
for kid in kids:
    live.add(kid)
print("registered", len(kids))
live.close()
print("return codes", [kid.poll() for kid in kids])
`);

  assert.match(out, /registered 2/);
  // Both reaped: a poll() of None would mean one was left running.
  assert.doesNotMatch(out, /None/);
});

test("a terminated bridge takes the server it spawned", async (t) => {
  const dir = await temp(t);
  const bridge = spawn("python3", ["-c",
    "import sys; sys.path.insert(0, 'lib/wrappers')\n" +
    "from broker.mcpbridge import main; sys.exit(main())",
    "serve", "demo", "--key", "k", "--", "sleep", "300"],
    { cwd: root, env: { ...process.env, HOME: dir, PYTHONDONTWRITEBYTECODE: "1" } });
  t.after(() => { try { bridge.kill("SIGKILL"); } catch {} });

  const state = path.join(dir, ".config", "broker", "box", "mcp", "demo-k.json");
  const said = await until(() => {
    try { return JSON.parse(require("node:fs").readFileSync(state, "utf8")); }
    catch { return null; }
  });
  assert.ok(said, "the listener never wrote its state file");

  // The greeting is the whole handshake: a wrong one spawns nothing at all.
  const client = net.connect(said.port, said.host);
  await new Promise((done, fail) => { client.on("connect", done); client.on("error", fail); });
  client.write(said.token + "\n");

  const server = await until(() => childrenOf(bridge.pid)[0] || null);
  assert.ok(server, "the listener never spawned a server");
  assert.ok(alive(server));

  bridge.kill("SIGTERM");
  const gone = await until(() => !alive(server) || null, 100, 100);
  assert.ok(gone, "the spawned server outlived the bridge that owned it");
  client.destroy();
});
