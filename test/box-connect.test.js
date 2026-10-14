// `broker box connect`: the supported way for a container that is NOT a box to
// reach an MCP server that stayed on the host. Crew asked for it because it is
// removing python from its image — so the connector it gets has to be node, and
// nothing about the listener, the secret or the allowed root may move to it.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFileSync, spawn } = require("node:child_process");
const { root, temp } = require("./helpers");

// The CLI, as a person types it. Its own HOME, so a test never reads or writes
// the config of the machine it runs on.
function cli(home, args, options = {}) {
  const env = { ...process.env, HOME: home, BROKER_REAL_HOME: home, PYTHONDONTWRITEBYTECODE: "1" };
  for (const name of Object.keys(env)) if (name.startsWith("HERDR_")) delete env[name];
  return execFileSync("node", [path.join(root, "cli.js"), ...args],
    { cwd: root, encoding: "utf8", env, ...options });
}

function stopListeners(home) {
  try {
    execFileSync("python3", ["-c",
      "import glob, json, os, signal, sys;" +
      "[os.kill(json.load(open(f))['pid'], signal.SIGTERM) for f in glob.glob(sys.argv[1])]",
      path.join(home, ".config", "broker", "box", "mcp", "*.json")], { stdio: "ignore" });
  } catch (_e) { /* a listener that is already gone needs no stopping */ }
}

test("box connect writes a node connector that speaks the bridge's own protocol", async (t) => {
  const home = await temp(t);
  t.after(() => stopListeners(home));
  const out = path.join(home, "run-1", "cbm");
  await fs.mkdir(path.dirname(out), { recursive: true });

  // `cat` stands in for an MCP server, as it does in the bridge's own test.
  const printed = cli(home, ["box", "connect", "probe", "--out", out, "--lang", "js",
    "--root", home, "--env", "PROBE_ROOT=" + home, "--", "cat"]).trim();
  assert.equal(printed, out, "it prints the path it wrote, for a script to capture");

  assert.equal((await fs.stat(out)).mode & 0o777, 0o700, "it carries the connection secret");
  const body = await fs.readFile(out, "utf8");
  assert.match(body, /^#!\/usr\/bin\/env node$/m, "the container runs it with node, not python");
  assert.doesNotMatch(body, /require\("child_process"\)/, "a connector spawns nothing");
  assert.match(body, /host\.docker\.internal/, "it reaches the host from inside a container");
  // node --check is the only honest answer to "is this valid JavaScript".
  execFileSync("node", ["--check", out]);

  // Now make it talk. The file names host.docker.internal, which resolves
  // inside a container and not here, so the host-side run reads the same file
  // with the address swapped: the handshake and the pumping are what is tested.
  const local = out + ".local.js";
  await fs.writeFile(local, body.replace(/host\.docker\.internal/g, "127.0.0.1"), { mode: 0o700 });
  const said = await new Promise((resolve, reject) => {
    const child = spawn("node", [local], { stdio: ["pipe", "pipe", "pipe"] });
    let seen = "", bad = "";
    child.stdout.on("data", (d) => { seen += d; });
    child.stderr.on("data", (d) => { bad += d; });
    child.on("error", reject);
    child.on("close", (code) => resolve({ seen, bad, code }));
    child.stdin.end("first line\nsecond line\n");
    setTimeout(() => child.kill(), 15000);
  });
  assert.equal(said.bad, "", "a working connector says nothing on stderr");
  assert.equal(said.code, 0, "stdin's end closes it cleanly");
  assert.equal(said.seen, "first line\nsecond line\n", "stdio crosses both ways, in order");

  // The secret is what stands between the loopback port and everything else on
  // the machine, so a connector that does not know it gets nothing.
  const wrong = out + ".wrong.js";
  await fs.writeFile(wrong, body.replace(/host\.docker\.internal/g, "127.0.0.1")
    .replace(/TOKEN = "[^"]*"/, 'TOKEN = "wrong-secret"'), { mode: 0o700 });
  const refused = await new Promise((resolve) => {
    const child = spawn("node", [wrong], { stdio: ["pipe", "pipe", "pipe"] });
    let seen = "";
    child.stdout.on("data", (d) => { seen += d; });
    child.on("close", (code) => resolve({ seen, code }));
    child.stdin.end("should not come back\n");
    setTimeout(() => child.kill(), 15000);
  });
  assert.equal(refused.seen, "", "no secret, no server, no answer");
  assert.notEqual(refused.code, 0, "and it says so with its exit code");
});

test("box connect reuses one listener per caller identity, and the python connector still works", async (t) => {
  const home = await temp(t);
  t.after(() => stopListeners(home));
  const port = async (file) => {
    const body = await fs.readFile(file, "utf8");
    // python says  HOST, PORT, TOKEN = 'host', 41234, 'secret'
    // node says    const HOST = "host", PORT = 41234, TOKEN = "secret";
    return Number(body.match(/PORT(?:, TOKEN)? = (?:'[^']*', )?(\d+)/)[1]);
  };

  const mine = path.join(home, "a");
  const again = path.join(home, "b");
  const other = path.join(home, "c");
  cli(home, ["box", "connect", "probe", "--out", mine, "--lang", "js",
    "--root", home, "--env", "PROBE_ROOT=" + home, "--", "cat"]);
  cli(home, ["box", "connect", "probe", "--out", again, "--lang", "js",
    "--root", home, "--env", "PROBE_ROOT=" + home, "--", "cat"]);
  cli(home, ["box", "connect", "probe", "--out", other, "--lang", "js",
    "--root", "/somewhere/else", "--env", "PROBE_ROOT=/somewhere/else", "--", "cat"]);

  assert.equal(await port(mine), await port(again), "one listener per caller, reused");
  assert.notEqual(await port(mine), await port(other),
    "a different allowed root is a different caller and gets its own listener");

  // The default is unchanged: a box still gets the python connector it had.
  const legacy = path.join(home, "d");
  cli(home, ["box", "connect", "probe", "--out", legacy, "--root", home,
    "--env", "PROBE_ROOT=" + home, "--", "cat"]);
  const body = await fs.readFile(legacy, "utf8");
  assert.match(body, /^#!\/usr\/bin\/env python3$/m, "py is still the default");
  assert.equal(await port(legacy), await port(mine), "and the language is not part of the identity");

  // Everything after `--` is the server's own argv, flags included. A server
  // that takes --lang of its own must not have it eaten on the way through.
  cli(home, ["box", "connect", "passthrough", "--out", path.join(home, "e"),
    "--lang", "js", "--", "cat", "--lang", "sh"]);
  const states = path.join(home, ".config", "broker", "box", "mcp");
  // The state file is <name>.json when nothing keys the identity, and
  // <name>-<identity>.json when something does.
  const recorded = await Promise.all((await fs.readdir(states))
    .filter((f) => f === "passthrough.json" || f.startsWith("passthrough-"))
    .map(async (f) => JSON.parse(await fs.readFile(path.join(states, f), "utf8")).command));
  assert.deepEqual(recorded, [["cat", "--lang", "sh"]], "the server's flags reach the server");
});

test("box connect refuses what it cannot do safely", async (t) => {
  const home = await temp(t);
  const refuses = (args, expected) => {
    assert.throws(() => cli(home, ["box", "connect", ...args], { stdio: "pipe" }),
      (e) => { assert.match(String(e.stderr), expected); return true; });
  };
  refuses(["probe", "--out", path.join(home, "x"), "--lang", "sh", "--", "cat"], /--lang takes 'py' or 'js'/);
  refuses(["probe", "--lang", "js", "--", "cat"], /needs --out/);
  refuses(["probe", "--out", path.join(home, "x"), "--lang", "js"], /needs the server's command/);
  refuses(["--out", path.join(home, "x"), "--", "cat"], /needs the server's name/);
  refuses(["../escape", "--out", path.join(home, "x"), "--", "cat"], /is not a server name/);
  refuses(["probe", "--out", path.join(home, "x"), "--env", "NOT A NAME=1", "--", "cat"], /is not a variable name/);
  refuses(["probe", "--out", path.join(home, "x"), "--env", "bare", "--", "cat"], /--env takes NAME=VALUE/);
  refuses(["probe", "--out", path.join(home, "x"), "--nonsense", "--", "cat"], /unknown flag for box connect/);
  // Nothing was written by any of those.
  await assert.rejects(fs.stat(path.join(home, "x")));
});

// Two callers differing ONLY in their root. The root arrives through --root and
// is in no environment, so nothing would tell them apart unless the broker puts
// it into the identity itself — and a shared listener would answer one caller's
// questions about the other's code. This used to hold because one MCP server's
// variable name was written into connect_mcp.
test("a different root alone is a different caller, with no flag to remember", async (t) => {
  const home = await temp(t);
  t.after(() => stopListeners(home));
  const port = async (file) =>
    Number((await fs.readFile(file, "utf8")).match(/PORT(?:, TOKEN)? = (?:'[^']*', )?(\d+)/)[1]);

  const here = path.join(home, "here");
  const there = path.join(home, "there");
  await fs.mkdir(here);
  await fs.mkdir(there);
  const run = (out, root) => cli(home, ["box", "connect", "probe", "--out", out,
    "--lang", "js", "--root", root, "--", "cat"]);

  run(path.join(home, "r1"), here);
  run(path.join(home, "r2"), here);
  run(path.join(home, "r3"), there);

  assert.equal(await port(path.join(home, "r1")), await port(path.join(home, "r2")),
    "same root, same listener");
  assert.notEqual(await port(path.join(home, "r1")), await port(path.join(home, "r3")),
    "a different root gets its own listener");
});
