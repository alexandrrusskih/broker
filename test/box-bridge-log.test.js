// What a bridged server says about itself, and what happens when it keeps
// dying. Both were measured on one machine: mcp-remote asked to be signed in
// fourteen hundred times over ten hours, every one of those lines went to
// DEVNULL, and nothing counted the doomed starts.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");
const { engine } = require("./helpers");

// The listener is started the way box/mcp.py starts it, so the test exercises
// the stderr it is really given rather than a stand-in for it.
function bridge(t, dir, command) {
  const env = { HOME: dir };
  for (const name of Object.keys(process.env)) if (name.startsWith("HERDR_")) env[name] = "";
  const out = engine(`
import json
from broker.box import mcp
server = {"command": ${JSON.stringify(command)}, "env": {}, "inherit": []}
print(json.dumps(mcp._start_bridge("probe", server, {}, []) or {}))
`, env);
  const live = JSON.parse(out.trim().split("\n").pop());
  assert.ok(live.port, "the listener should come up and publish its port");
  t.after(() => { try { process.kill(live.pid); } catch { /* already gone */ } });
  return live;
}

// One client, one spawned server. Returns whatever came back on the wire.
function talk(live, line = "") {
  return new Promise((resolve) => {
    const sock = net.connect(live.port, live.host || "127.0.0.1");
    let seen = "";
    sock.on("connect", () => sock.write(`${live.token}\n${line}`));
    // Whichever comes first: an answer, or the server going away without one.
    sock.on("data", (d) => { seen += d; if (seen.includes("\n")) { sock.end(); resolve(seen.trim()); } });
    sock.on("close", () => resolve(seen.trim()));
    sock.on("error", () => resolve(seen.trim()));
    setTimeout(() => { sock.destroy(); resolve(seen.trim()); }, 5000);
  });
}

async function until(what, tries = 100, pause = 50) {
  for (let i = 0; i < tries; i++) {
    const answer = await what();
    if (answer) return answer;
    await new Promise((done) => setTimeout(done, pause));
  }
  return null;
}

test("a server asking to be signed in is written down, not discarded", async (t) => {
  const dir = await require("./helpers").temp(t);
  const asking = "Please authorize this client by visiting: https://example.invalid/auth";
  // Says its line and then behaves like a server, so the session is ordinary.
  const live = bridge(t, dir, ["/bin/sh", "-c", `echo '${asking}' >&2; cat`]);
  await talk(live, "hello\n");

  const log = path.join(dir, ".config", "broker", "box", "mcp", "probe.log");
  const text = await until(async () => {
    try { const seen = await fs.readFile(log, "utf8"); return seen.includes(asking) ? seen : null; }
    catch { return null; }
  });
  assert.ok(text, `the bridge log should hold what the server printed:\n${log}`);
  // It holds a credential often enough to be treated as one.
  assert.equal((await fs.stat(log)).mode & 0o777, 0o600, "the bridge log is not world-readable");
});

test("a command that dies at once is left alone, and the client is told why", async (t) => {
  const dir = await require("./helpers").temp(t);
  const live = bridge(t, dir, ["/bin/sh", "-c", "echo 'it cannot start' >&2; exit 3"]);

  // FAST_FAIL_LIMIT starts, each one over before FAST_FAIL_SECONDS.
  for (let i = 0; i < 5; i++) assert.equal(await talk(live), "", `start ${i + 1} spawns and dies`);

  const refused = await until(async () => {
    const said = await talk(live);
    return said.includes("not being started again") ? said : null;
  }, 20, 100);
  assert.ok(refused, "the next client should be refused rather than handed another corpse");
  const answer = JSON.parse(refused);
  assert.match(answer.error, /probe stopped at once \d+ times in a row/);
  assert.match(answer.error, /probe\.log/, "the refusal names where the server's own words are");

  // And the reason is in the log, which is the point of naming it.
  const log = path.join(dir, ".config", "broker", "box", "mcp", "probe.log");
  assert.match(await fs.readFile(log, "utf8"), /it cannot start/);
});

test("a server that runs clears the failures it had before", async (t) => {
  const dir = await require("./helpers").temp(t);
  const out = engine(`
import time
from broker import mcpserve
command = ["/bin/sh", "-c", "exit 1"]
for _ in range(6):
    mcpserve._breaker_note(command, 0.1, True, False)
print("cooling:", bool(mcpserve._cooling(command, {"name": "probe", "log": "probe.log"})))
# One start that actually ran. A server that was down and came back must not be
# held off for something it no longer does.
mcpserve._breaker_note(command, mcpserve.FAST_FAIL_SECONDS + 1, True, False)
print("cooling:", bool(mcpserve._cooling(command, {"name": "probe", "log": "probe.log"})))
`, { HOME: dir });
  assert.deepEqual(out.trim().split("\n"), ["cooling: True", "cooling: False"]);
});

test("a short session that the server answered is not a failed start", async (t) => {
  const dir = await require("./helpers").temp(t);
  // A stdio server exits when its stdin closes, so connect-ask-close ends with
  // the server gone in well under a second, by its own hand. It answered, so it
  // started, and five of these must not hold it off.
  const live = bridge(t, dir, ["/bin/sh", "-c", "read line; echo \"$line\""]);
  for (let i = 0; i < 6; i++) assert.equal(await talk(live, "hello\n"), "hello", `session ${i + 1}`);
  const said = await talk(live, "hello\n");
  assert.equal(said, "hello", "still answering, with nothing held against it");
  assert.equal(said.includes("not being started again"), false);
});

test("the breaker can be switched off", async (t) => {
  const dir = await require("./helpers").temp(t);
  const out = engine(`
from broker import mcpserve
command = ["/bin/sh", "-c", "exit 1"]
for _ in range(20):
    mcpserve._breaker_note(command, 0.1, True, False)
print("cooling:", bool(mcpserve._cooling(command)))
`, { HOME: dir, BROKER_MCP_BREAKER: "0" });
  assert.equal(out.trim(), "cooling: False");
});
