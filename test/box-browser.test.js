const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const engine = path.join(__dirname, "..", "lib", "wrappers");

// A temporary HOME with a fake `open` on PATH, so the bridge's host end can be
// watched without a window appearing on anybody's screen.
async function machine(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "broker-browser-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const project = path.join(home, "project");
  const bin = path.join(home, "bin");
  const opened = path.join(home, "opened");
  await fs.mkdir(project);
  await fs.mkdir(bin);
  for (const name of ["open", "xdg-open"]) {
    const fake = path.join(bin, name);
    await fs.writeFile(fake, `#!/bin/sh\necho "$@" >> ${JSON.stringify(opened)}\n`);
    await fs.chmod(fake, 0o700);
  }
  const env = {
    ...process.env, HOME: home, BROKER_REAL_HOME: home,
    PATH: `${bin}:${process.env.PATH}`, PYTHONDONTWRITEBYTECODE: "1",
  };
  // The listener outlives the run that started it, by design — so a test that
  // starts one has to put it down.
  t.after(async () => {
    try {
      const file = path.join(home, ".config", "broker", "box", "browser", "demo.json");
      const state = JSON.parse(await fs.readFile(file, "utf8"));
      process.kill(state.pid, "SIGTERM");
    } catch { /* never came up, or already gone */ }
  });
  return { home, project, env, opened };
}

function mounts(profile, { project, env }) {
  const code = `
import json, sys
sys.path.insert(0, ${JSON.stringify(engine)})
from broker.box import extras, run
from broker.providers import codex
extras._passwd_file = lambda *args: None
profile = json.loads(sys.argv[1])
profile["rw"] = [${JSON.stringify(project)}]
profile["mcp"] = False
cmd = run.command(codex, "demo", profile, [], {})
print(json.dumps(cmd))
`;
  return JSON.parse(execFileSync("python3", ["-c", code, JSON.stringify(profile)],
    { cwd: path.join(__dirname, ".."), encoding: "utf8", env }));
}

test("a box gets no browser unless it asks for one", async (t) => {
  const box = await machine(t);
  const cmd = mounts({}, box);
  assert.ok(!cmd.some((a) => a.includes("/usr/bin/open")),
    "the person's own browser session is not part of being a box");
});

test("a box that asks gets a stand-in for open, and the host keeps the rule", async (t) => {
  const box = await machine(t);
  const cmd = mounts({ browser: ["admin-staging.finik.ai", ".finik.ai"] }, box);

  // Both names a caller reaches for, read-only, and the same shim behind them.
  const shims = cmd.filter((a) => a.startsWith("type=bind,")
    && /target=\/usr\/bin\/(xdg-)?open,readonly$/.test(a));
  assert.equal(shims.length, 2, "open and xdg-open both stand in");
  const sources = new Set(shims.map((m) => m.match(/source=([^,]+)/)[1]));
  assert.equal(sources.size, 1);
  // The shim dials the host by the gateway name, so the box must be able to
  // resolve it even with no MCP server claimed.
  assert.ok(cmd.includes("host.docker.internal:host-gateway"),
    "the box must be able to resolve the gateway it dials");

  const shim = [...sources][0];
  const body = await fs.readFile(shim, "utf8");
  assert.match(body, /host\.docker\.internal/);
  assert.doesNotMatch(body, /subprocess|Popen/, "the box end only carries the URL");

  // The round trip, against the listener that run.command started. The shim
  // would run inside the container; here it runs with the gateway name pointed
  // at loopback, which is what that name means on the far side.
  const dial = (url) => {
    const code = `
import socket, sys
real = socket.getaddrinfo
socket.getaddrinfo = lambda host, *a, **k: real("127.0.0.1", *a, **k)
sys.argv = ["open"] + sys.argv[1:]
exec(open(${JSON.stringify(shim)}).read())
`;
    try {
      return { code: 0, out: execFileSync("python3", ["-c", code, url],
        { encoding: "utf8", env: box.env, stdio: ["ignore", "pipe", "pipe"] }) };
    } catch (error) {
      return { code: error.status, out: String(error.stdout || ""),
        err: String(error.stderr || "") };
    }
  };

  const good = dial("https://admin-staging.finik.ai/compliance/kyt-categories");
  assert.equal(good.code, 0, good.err);
  assert.match(good.out, /opened on the host in Google Chrome/);

  const refused = dial("https://evil.example/steal");
  assert.equal(refused.code, 1);
  assert.match(refused.err, /this box may open/);

  const local = dial("file:///etc/passwd");
  assert.equal(local.code, 1);
  assert.match(local.err, /only http and https/);

  // Only the allowed address reached a browser, and it reached the named one.
  const log = await fs.readFile(box.opened, "utf8");
  assert.equal(log.trim().split("\n").length, 1);
  assert.match(log, /-a Google Chrome -- https:\/\/admin-staging\.finik\.ai\//);

  // Every request is answerable for afterwards, refusals included.
  const audit = await fs.readFile(
    path.join(box.home, ".config", "broker", "box", "browser.log"), "utf8");
  assert.equal((audit.match(/\topened\t/g) || []).length, 1);
  assert.equal((audit.match(/\trefused\t/g) || []).length, 2);
});
