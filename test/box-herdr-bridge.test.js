// The terminal manager runs on the host and only the command crosses into the
// box. The port and the secret are written INTO the stand-in when the box
// starts, so the listener has to outlast every silence that box may keep: one
// that gives up leaves the box holding a dead port and no way to learn a new
// one. These pin that, and the two things the host end must refuse.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { root, temp } = require("./helpers");

// Runs the listener in-process, with the real socket, and reports what it did.
// `asks` is a list of [token-or-null, argv] sent one after another.
function run(home, groups, asks, extra = "") {
  const body = `
import json, os, socket, sys, threading, time
sys.path.insert(0, ${JSON.stringify(path.join(root, "lib", "wrappers"))})

# Every timeout armed on any socket, so a listener that gives up on silence
# is visible from the outside.
armed = []
real = socket.socket.settimeout
socket.socket.settimeout = lambda self, value: (armed.append(value), real(self, value))[1]

from broker.box import herdr

name, binary = "boxy", os.path.join(os.environ["HOME"], "fake-herdr")
threading.Thread(target=herdr.serve, args=(name, binary, ${JSON.stringify(groups)}),
                 daemon=True).start()

state = None
for _ in range(100):
    state = herdr.live(name)
    if state:
        break
    time.sleep(0.05)
assert state, "the listener never came up"

said = []
for token, argv in json.loads(${JSON.stringify(JSON.stringify(asks))}):
    conn = socket.create_connection(("127.0.0.1", state["port"]), timeout=10)
    ask = {"token": state["token"] if token is None else token, "argv": argv}
    conn.sendall(json.dumps(ask).encode() + b"\\n")
    conn.shutdown(socket.SHUT_WR)
    body = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    conn.close()
    said.append(json.loads(body.decode() or "null"))

print(json.dumps({"said": said, "armed": armed, "groups": state["groups"],
                  "started": bool(state.get("started"))}))
${extra}
`;
  return JSON.parse(execFileSync("python3", ["-c", body], {
    encoding: "utf8",
    env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: "1" }
  }).trim().split("\n").pop());
}

// A stand-in for herdr itself: it says what it was given, and leaves a mark on
// disk, so a call that should never have happened can be proven not to have.
async function fakeHerdr(home) {
  const file = path.join(home, "fake-herdr");
  await fs.writeFile(file,
    `#!/bin/sh\nprintf 'ran %s\\n' "$*"\nprintf 'warned\\n' >&2\n` +
    `printf '%s\\n' "$*" >> ${path.join(home, "calls")}\n`);
  await fs.chmod(file, 0o700);
  return file;
}

test("the listener does not give up on silence, because the box cannot be told a new port", async (t) => {
  const home = await temp(t);
  await fakeHerdr(home);

  const out = run(home, ["*"], [[null, ["tab", "list"]], [null, ["pane", "list"]]]);

  assert.equal(out.said[0].code, 0, "the first call went through");
  assert.match(out.said[0].out, /ran tab list/);
  assert.equal(out.said[1].code, 0, "and so did a later one");
  // The regression itself: an hour-long idle timeout on the listening socket
  // closed it while the box was still running, and the box held a port that
  // nothing answered. A box lives for days; its silence is not its death.
  assert.ok(!out.armed.includes(3600),
    `no idle timeout is armed on the listener, got ${JSON.stringify(out.armed)}`);
  assert.ok(out.started, "the listener records its start time, so a recycled pid is not mistaken for it");
});

test("a caller without the secret is told nothing and runs nothing", async (t) => {
  const home = await temp(t);
  await fakeHerdr(home);

  const out = run(home, ["*"], [["wrong-secret", ["tab", "list"]]]);

  assert.equal(out.said[0], null, "the connection is closed without a word");
  await assert.rejects(fs.access(path.join(home, "calls")),
    "and herdr was never run: the port is open to every process on the machine");
});

test("a box is held to the groups it was given, and a bare herdr is refused outright", async (t) => {
  const home = await temp(t);
  await fakeHerdr(home);

  const out = run(home, ["tab"], [
    [null, ["tab", "list"]],
    [null, ["agent", "read", "finik/lane5"]],
    [null, []],
    [null, ["--json", "tab", "list"]]
  ]);

  assert.equal(out.said[0].code, 0, "the group it was given works");
  assert.equal(out.said[1].code, 1, "one it was not given does not");
  assert.match(out.said[1].err, /may run tab, not agent/);
  // `herdr agent read` shows any agent's screen and `pane process-info` prints
  // command lines carrying the fleet's tokens. A box that was given "tab" must
  // not reach either.
  assert.equal(out.said[2].code, 1, "a bare herdr opens a terminal the box has nothing to draw on");
  assert.match(out.said[2].err, /a bare herdr/);
  assert.equal(out.said[3].code, 0, "a flag before the subcommand decides nothing");

  const calls = await fs.readFile(path.join(home, "calls"), "utf8");
  assert.ok(!calls.includes("agent"), "the refused command never reached herdr");
});

test("a recycled pid is not mistaken for our listener", async (t) => {
  const home = await temp(t);
  const file = path.join(home, ".config", "broker", "box", "herdr", "boxy.json");
  await fs.mkdir(path.dirname(file), { recursive: true });
  // Our own pid, so it is certainly alive — with a start time that is not ours.
  await fs.writeFile(file, JSON.stringify({
    port: 1, token: "t", pid: process.pid, groups: ["*"],
    started: "Thu Jan  1 00:00:00 1970"
  }));

  const said = execFileSync("python3", ["-c",
    `import sys; sys.path.insert(0, ${JSON.stringify(path.join(root, "lib", "wrappers"))});` +
    "from broker.box import herdr; print(herdr.live('boxy') is None)"
  ], { encoding: "utf8", env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: "1" } });

  assert.equal(said.trim(), "True",
    "alive is not the same as ours: a box would send its arguments to a stranger");
});
