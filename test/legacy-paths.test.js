// DROP AFTER 2026-12 — this whole file goes with the paths it covers.
//
// The tool was called hltm-broker until September 2026. Three things on disk
// outlive that name and cannot simply be abandoned: the config, the directory
// holding binaries the shims moved aside, and the registered system service.
// Each is carried across exactly once, and each is tested here so the carry is
// not quietly broken long before the day it is needed.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { writeJson, readJson } = require("../functions/stores/files");
const root = path.join(__dirname, "..");

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-legacy-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  return dir;
}

test("a config left under the old name is still read, by both the CLI and the engine", async (t) => {
  const home = await temp(t);
  const legacy = path.join(home, ".config", "hltm-broker", "config.json");
  await writeJson(legacy, { url: "https://saved.example.test", key: "fake-legacy-key", account: "main" });

  const env = { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: "1" };
  delete env.BROKER_CONFIG;
  delete env.BROKER_URL;
  delete env.BROKER_KEY;

  const fromNode = JSON.parse(execFileSync(process.execPath,
    ["-e", "process.stdout.write(JSON.stringify(require('./lib/config').require()))"], { cwd: root, env }));
  assert.equal(fromNode.key, "fake-legacy-key");
  assert.equal(fromNode.account, "main");

  const fromPython = JSON.parse(execFileSync("python3",
    ["-c", "import sys,json; sys.path.insert(0,'lib/wrappers'); from broker import config; print(json.dumps(config.load(sys.exit)))"],
    { cwd: root, env }));
  assert.equal(fromPython.key, "fake-legacy-key");

  // Reading it copies it across; the old file stays where it is, so a machine
  // still running an older wrapper keeps working.
  assert.equal((await readJson(path.join(home, ".config", "broker", "config.json"))).key, "fake-legacy-key");
  assert.equal((await readJson(legacy)).key, "fake-legacy-key");
});

test("stashed binaries move with the engine directory, and what points at them moves too", async (t) => {
  const home = await temp(t);
  const stashed = path.join(home, ".local", "lib", "hltm-broker", "real", "agy");
  await fs.mkdir(path.dirname(stashed), { recursive: true });
  await fs.writeFile(stashed, "#!/bin/sh\n# pretending to be 178 MB of agy\n", { mode: 0o755 });
  await writeJson(path.join(home, ".config", "broker", "config.json"), {
    url: "https://saved.example.test", key: "fake-key",
    shim_previous: { agy: stashed, codex: "/usr/local/bin/codex" }
  });

  const env = { ...process.env, HOME: home };
  delete env.BROKER_CONFIG;
  execFileSync(process.execPath, ["-e", "require('./lib/wrap').install('agy', {})"], { cwd: root, env });

  const moved = path.join(home, ".local", "lib", "broker", "real", "agy");
  assert.equal(await fs.readFile(moved, "utf8"), "#!/bin/sh\n# pretending to be 178 MB of agy\n");
  await assert.rejects(fs.stat(path.join(home, ".local", "lib", "hltm-broker")), { code: "ENOENT" });

  // Without this, `broker agy remove` looks for the binary where it no longer is
  // and refuses to restore anything.
  const cfg = await readJson(path.join(home, ".config", "broker", "config.json"));
  assert.equal(cfg.shim_previous.agy, moved);
  assert.equal(cfg.shim_previous.codex, "/usr/local/bin/codex", "paths outside the stash are left alone");
});

test("an already-registered service is adopted under the new label, never left running beside it", async (t) => {
  const dir = await temp(t);
  const legacyRoot = path.join(dir, "hltm-broker-service");
  const newRoot = path.join(dir, "broker-service");
  const legacyPlist = path.join(dir, "com.hltm.broker.plist");
  const dataDir = path.join(dir, "data");
  await writeJson(path.join(legacyRoot, "service.json"),
    { user: os.userInfo().username, runtime: path.join(legacyRoot, "runtime"), dataDir, port: 8787, url: "https://saved.example.test" });
  await fs.mkdir(path.join(legacyRoot, "runtime"), { recursive: true });
  await fs.writeFile(legacyPlist, "<plist/>");

  const calls = [];
  const manager = require("../lib/service").createServiceManager({
    root: newRoot,
    plist: path.join(dir, "com.broker.server.plist"),
    legacyRoot,
    legacyPlist,
    platform: "darwin",
    user: { username: os.userInfo().username, uid: 501 },
    startTimeoutMs: 1,
    stopTimeoutMs: 1,
    sleep: () => Promise.resolve(),
    exec: (bin, args) => {
      calls.push([bin, ...args].join(" "));
      if (bin === "/bin/launchctl" && args[0] === "print") return "  pid = 4242\n";
      return "";
    }
  });

  // install() goes on to do real work; the adoption is what this test is about,
  // so let it fail afterwards and check what happened before it did.
  await manager.install({ url: "https://saved.example.test" }).catch(() => {});

  assert.ok(calls.some((c) => c.includes("bootout system/com.hltm.broker")),
    "the old daemon must be stopped, or KeepAlive holds the port against its replacement");
  assert.ok(calls.some((c) => c.includes("/bin/rm -f " + legacyPlist)), "the old registration must be removed");
  await assert.rejects(fs.stat(legacyRoot), { code: "ENOENT" });
  // The data directory is NOT moved: live tokens stay put, and service.json is
  // corrected to point at the code's new home.
  const carried = await readJson(path.join(newRoot, "service.json"));
  assert.equal(carried.dataDir, dataDir);
  assert.equal(carried.runtime, path.join(newRoot, "runtime"));
});
