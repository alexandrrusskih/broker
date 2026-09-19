const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const syncFs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { createServiceManager, renderPlist } = require("../lib/service");
const { writeJson, readJson } = require("../functions/stores/files");

async function fixture(t, overrides = {}) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-service-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const root = path.join(dir, "service");
  const runtime = path.join(root, "runtime");
  const plist = path.join(dir, "system.plist");
  const dataDir = path.join(dir, "data");
  const calls = [];
  const state = { loaded: false, starts: 0, draining: 0, preflightFails: false };
  const deps = {
    root, plist, platform: "darwin", user: { username: "test-operator", uid: 501 },
    node: process.execPath, openPrompter: () => null,
    startTimeoutMs: 0, stopTimeoutMs: 1000, sleep: async () => {},
    probe: async () => true,
    alive() {
      if (state.draining > 0) {
        state.onDrain?.();
        state.draining--;
        return true;
      }
      return false;
    },
    exec(bin, args) {
      calls.push([bin, ...args]);
      if (bin === process.execPath) {
        if (state.preflightFails) throw new Error("bad new server code");
        return execFileSync(bin, args, { stdio: "pipe" });
      }
      if (bin === "/usr/bin/plutil") {
        if (process.platform === "darwin") return execFileSync(bin, args, { stdio: "pipe" });
        return;
      }
      if (bin === "/bin/launchctl") {
        assert.deepEqual(args, ["print", "system/com.broker.server"]);
        if (!state.loaded) throw Object.assign(new Error("not loaded"), { status: 113 });
        return `system/com.broker.server = {\n  pid = ${1000 + state.starts}\n}`;
      }
      assert.equal(bin, "/usr/bin/sudo");
      if (args[0] === "/usr/bin/install") {
        syncFs.copyFileSync(args.at(-2), args.at(-1));
        return;
      }
      assert.equal(args[0], "/bin/launchctl");
      if (args[1] === "bootout") {
        assert.equal(args[2], "system/com.broker.server");
        state.loaded = false;
        state.draining = 2;
      } else if (args[1] === "bootstrap") {
        assert.deepEqual(args.slice(1), ["bootstrap", "system", plist]);
        assert.equal(state.draining, 0, "the old process must finish before the next starts");
        state.loaded = true;
        state.starts++;
      } else assert.deepEqual(args.slice(1), ["enable", "system/com.broker.server"]);
    },
    ...overrides
  };
  const manager = createServiceManager(deps);
  const options = { from: path.join(__dirname, ".."), dataDir, url: "https://broker.example.test", noAsk: true };
  return { dir, root, runtime, plist, dataDir, calls, state, deps, manager, options };
}

test("system plist escapes paths and uses an unprivileged user, loopback and a drain timeout", () => {
  const xml = renderPlist({ user: "operator", node: "/opt/node", runtime: "/private/A&B", dataDir: "/private/C<D", port: 9000 });
  assert.match(xml, /<key>UserName<\/key><string>operator<\/string>/);
  assert.match(xml, /A&amp;B\/cli.js/);
  assert.match(xml, /C&lt;D/);
  assert.match(xml, /<string>9000<\/string>/);
  assert.match(xml, /127\.0\.0\.1/);
  assert.match(xml, /ExitTimeOut<\/key><integer>120/);
  assert.doesNotMatch(xml, /REPLACE|gui\//);
});

test("install snapshots server code, exports separate connections and preserves tokens on upgrade", async (t) => {
  const f = await fixture(t);
  const first = await f.manager.install(f.options);
  assert.equal(first.updated, false);
  assert.equal(first.runtime, f.runtime);
  assert.equal((await fs.lstat(f.runtime)).isSymbolicLink(), false);
  assert.equal((await fs.stat(first.clientFile)).mode & 0o777, 0o600);
  assert.equal((await fs.stat(path.join(f.root, "service.json"))).mode & 0o777, 0o600);
  const settings = await readJson(path.join(f.dataDir, "settings.json"));
  assert.equal((await readJson(first.clientFile)).key, settings.client_key);
  assert.equal((await readJson(first.adminFile)).key, settings.broker_key);
  assert.equal((await readJson(first.adminFile)).url, "http://127.0.0.1:8787");
  assert.notEqual(settings.client_key, settings.broker_key);
  await assert.rejects(fs.stat(path.join(f.runtime, ".git")), { code: "ENOENT" });
  await assert.rejects(fs.stat(path.join(f.runtime, "lib", "wrappers", "broker", "config.py")), { code: "ENOENT" });
  const account = path.join(f.dataDir, "accounts", "_codex", "_main.json");
  await writeJson(account, { refresh_token: "fake-current-token" });
  const marker = path.join(f.runtime, "old-code.txt");
  await fs.writeFile(marker, "old");
  f.state.onDrain = () => assert.equal(syncFs.readFileSync(marker, "utf8"), "old");
  const second = await f.manager.install({ from: f.options.from, noAsk: true });
  assert.equal(second.updated, true);
  assert.deepEqual(await readJson(path.join(f.dataDir, "settings.json")), settings);
  assert.equal((await readJson(account)).refresh_token, "fake-current-token");
  await assert.rejects(fs.stat(marker), { code: "ENOENT" });
  assert.equal((await f.manager.status()).healthy, true);
  assert.equal(f.calls.filter((c) => c.includes("bootout")).length, 1);
  assert.ok(f.calls.every((c) => !c.includes("kickstart")));
});

test("failed upgrade restores previous code but never rolls tokens back", async (t) => {
  let f;
  let rotated = false;
  f = await fixture(t, { probe: async () => {
    if (f.state.starts !== 2) return true;
    rotated = true;
    await writeJson(path.join(f.dataDir, "accounts", "_codex", "_main.json"), { refresh_token: "fake-rotated-token" });
    return false;
  } });
  await f.manager.install(f.options);
  const previous = await readJson(path.join(f.root, "service.json"));
  const marker = path.join(f.runtime, "old-code.txt");
  await fs.writeFile(marker, "old");
  await assert.rejects(f.manager.install({ ...f.options, port: 9001 }), /previous server runtime restored/);
  assert.equal(rotated, true);
  assert.equal(await fs.readFile(marker, "utf8"), "old");
  assert.equal((await readJson(path.join(f.dataDir, "accounts", "_codex", "_main.json"))).refresh_token, "fake-rotated-token");
  assert.deepEqual(await readJson(path.join(f.root, "service.json")), previous);
  assert.equal((await readJson(path.join(f.dataDir, "admin.json"))).url, "http://127.0.0.1:8787");
  assert.equal((await f.manager.status()).healthy, true);
  assert.equal(f.state.starts, 3);
});

test("new-code preflight failure does not stop the current server", async (t) => {
  const f = await fixture(t);
  await f.manager.install(f.options);
  f.state.preflightFails = true;
  await assert.rejects(f.manager.install(f.options), /bad new server code/);
  assert.equal(f.state.starts, 1);
  assert.equal(f.calls.filter((c) => c.includes("bootout")).length, 0);
  assert.equal((await f.manager.status()).healthy, true);
});

test("an interrupted first startup can be repaired without regenerating keys", async (t) => {
  let ready = false;
  const f = await fixture(t, { probe: async () => ready });
  await assert.rejects(f.manager.install(f.options), /did not become ready/);
  const settings = await readJson(path.join(f.dataDir, "settings.json"));
  ready = true;
  await f.manager.install(f.options);
  assert.deepEqual(await readJson(path.join(f.dataDir, "settings.json")), settings);
  assert.equal((await f.manager.status()).healthy, true);
});

test("stop/restart target system launchd and preserve registration", async (t) => {
  const f = await fixture(t);
  await f.manager.install(f.options);
  await f.manager.stop();
  assert.equal((await f.manager.status()).pid, null);
  assert.ok(await fs.stat(f.plist));
  await f.manager.restart();
  assert.equal((await f.manager.status()).healthy, true);
});

test("unsupported/root installs, unmanaged services and data migration fail without overwriting them", async (t) => {
  for (const [overrides, pattern] of [
    [{ platform: "linux" }, /supports macOS/],
    [{ user: { username: "root", uid: 0 } }, /not sudo\/root/]
  ]) {
    const f = await fixture(t, overrides);
    await assert.rejects(f.manager.install(f.options), pattern);
    assert.deepEqual(f.calls, []);
    assert.equal(await readJson(path.join(f.dataDir, "settings.json")), null);
  }
  const f = await fixture(t);
  await assert.rejects(f.manager.install({ ...f.options, dataDir: path.join(f.root, "runtime.previous") }), /outside the managed server code/);
  assert.equal(await readJson(path.join(f.root, "runtime.previous", "settings.json")), null);
  await fs.writeFile(f.plist, "unmanaged");
  await assert.rejects(f.manager.install(f.options), /unmanaged/);
  assert.equal(await fs.readFile(f.plist, "utf8"), "unmanaged");
  await fs.unlink(f.plist);
  await f.manager.install(f.options);
  await assert.rejects(f.manager.install({ ...f.options, dataDir: path.join(f.dir, "elsewhere") }), /migration/);
  assert.equal(f.calls.filter((c) => c.includes("bootout")).length, 0);
});
