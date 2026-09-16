const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const { setupContainer } = require("../lib/container-setup");
const { installContainer } = require("../lib/wrap");
const { readJson, writeJson } = require("../functions/stores/files");

async function fixture(t, saved = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "broker-container-test-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const calls = [];
  const root = path.join(home, ".medulla", "container");
  return {
    home, calls, root,
    file: path.join(root, "home", ".config", "hltm-broker", "config.json"),
    config: {
      FILE: path.join(home, ".config", "hltm-broker", "config.json"),
      read: () => ({ ...saved }),
      write: () => assert.fail("container setup must not change the host config")
    },
    installOverlay: (provider, destination) => {
      calls.push([provider, destination]);
      return path.join(destination, "bin", "broker-cx");
    }
  };
}

test("container setup uses the saved laptop client without machine paths or native tokens", async (t) => {
  const f = await fixture(t, {
    url: "https://broker.example.test/", key: "fake-client", role: "client",
    accounts: { codex: "second" }, min_headroom: 20,
    bin_dir: "/host/bin", shim_previous: { codex: "/host/native" },
    tokens: { refresh_token: "never-copy-this" }
  });
  const out = await setupContainer({}, f);
  assert.equal(out.url, "https://broker.example.test");
  assert.deepEqual(f.calls, [["codex", f.root]]);
  assert.deepEqual(await readJson(f.file), {
    url: out.url, key: "fake-client", role: "client",
    accounts: { codex: "second" }, min_headroom: 20
  });
  assert.equal((await fs.stat(f.file)).mode & 0o777, 0o600);
  assert.equal((await fs.stat(path.dirname(f.file))).mode & 0o777, 0o700);
});

test("existing overlays keep their connection; an explicit file or URL replaces it", async (t) => {
  const f = await fixture(t, { url: "https://host.test", key: "host-key" });
  await writeJson(f.file, { url: "https://container.test", key: "old-key", role: "client" });
  await setupContainer({}, f);
  assert.equal((await readJson(f.file)).url, "https://container.test");
  const supplied = path.join(f.home, "delivered client.json");
  await writeJson(supplied, { url: "https://new.test", key: "new-key", role: "client" });
  await setupContainer({ clientConfig: supplied }, f);
  assert.equal((await readJson(f.file)).key, "new-key");
  await setupContainer({ url: "https://alias.test/" }, f);
  assert.equal((await readJson(f.file)).url, "https://alias.test");
  assert.equal((await readJson(f.file)).key, "new-key");
  assert.equal(f.calls.length, 3, "repeated setup updates the wrapper too");
});

test("managed server loopback resolves to its own external CLIENT config, including a custom data dir", async (t) => {
  const f = await fixture(t, { url: "http://127.0.0.1:8787", key: "same-key", role: "client" });
  const dataDir = path.join(f.home, "custom data");
  await writeJson(path.join(f.home, ".local/share/hltm-broker-service/service.json"), { dataDir });
  await writeJson(path.join(dataDir, "client.json"), {
    url: "https://server.example.test", key: "same-key", role: "client"
  });
  // No settings.json/admin.json is present: neither is needed or read.
  const out = await setupContainer({}, f);
  assert.equal(out.source, path.join(dataDir, "client.json"));
  assert.deepEqual(await readJson(f.file), {
    url: "https://server.example.test", key: "same-key", role: "client"
  });
});

test("loopback never silently switches to an unrelated local server", async (t) => {
  const f = await fixture(t, { url: "http://127.0.0.1:9000", key: "another-key", role: "client" });
  const dataDir = path.join(f.home, "data");
  await writeJson(path.join(f.home, ".local/share/hltm-broker-service/service.json"), { dataDir });
  await writeJson(path.join(dataDir, "client.json"), { url: "https://wrong.test", key: "wrong-key", role: "client" });
  await assert.rejects(setupContainer({}, f), /loopback/);
  assert.deepEqual(f.calls, []);
  await setupContainer({ url: "https://right.test" }, f);
  assert.equal((await readJson(f.file)).key, "another-key");
});

test("admin, missing credentials and invalid destinations fail before installing anything", async (t) => {
  for (const saved of [
    { url: "https://broker.test", key: "admin-secret", role: "admin" },
    { url: "https://broker.test" },
    { url: "https://user:secret@broker.test", key: "fake" },
    { url: "file:///tmp/broker", key: "fake" },
    { url: "https://broker.test?key=secret", key: "fake" },
    { url: "http://localhost:8787", key: "fake" },
    { url: "http://127.1:8787", key: "fake" },
    { url: "http://[::1]:8787", key: "fake" }
  ]) {
    const f = await fixture(t, saved);
    await assert.rejects(setupContainer({}, f));
    assert.deepEqual(f.calls, []);
    assert.equal(await readJson(f.file), null);
  }
  const f = await fixture(t, { url: "https://broker.test", key: "client-key" });
  const admin = path.join(f.home, "admin.json");
  await writeJson(admin, { url: "https://broker.test", key: "admin-secret", role: "admin" });
  await assert.rejects(setupContainer({ clientConfig: admin }, f), /not the server's admin/);
  await assert.rejects(setupContainer({ clientConfig: path.join(f.home, "absent.json") }, f), /not found/);
  assert.deepEqual(f.calls, []);
});

test("real overlay builder produces a standalone bundle and home shim without a host installation", async (t) => {
  const f = await fixture(t);
  const bundle = installContainer("codex", f.root);
  assert.equal(bundle, path.join(f.root, "bin", "broker-cx"));
  assert.equal((await fs.stat(bundle)).mode & 0o777, 0o755);
  const shim = await fs.readFile(path.join(f.root, "home/.local/bin/codex"), "utf8");
  assert.match(shim, /hltm-broker shim/);
  assert.match(shim, /exec \/usr\/local\/bin\/broker-cx/);
  execFileSync("python3", ["-m", "zipfile", "-t", bundle], { stdio: "pipe" });
  const result = execFileSync("python3", ["-c",
    "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); assert 'hltm/version.py' in z.namelist(); assert '__main__.py' in z.namelist(); print('ok')",
    bundle], { encoding: "utf8" });
  assert.equal(result.trim(), "ok");
  assert.deepEqual(await fs.readdir(f.home), [".medulla"]);
  assert.equal(await readJson(f.file), null, "wrapper builder does not copy any credentials");
});

test("setup --container is opt-in and forwards the connection options without changing ordinary setup", async () => {
  const source = (await fs.readFile(path.join(__dirname, "../cli.js"), "utf8"))
    .replace("main().catch", "globalThis.done = main().catch");
  for (const container of [false, true]) {
    const calls = [];
    const context = {
      process: {
        argv: ["node", "cli.js", "setup", ...(container ? ["--container"] : []),
          "--client-config", "/test/client.json", "--url", "https://broker.test", "--no-ask"],
        stderr: { write(s) { assert.fail(s); } }, exit() { assert.fail("unexpected exit"); }
      },
      console: { log() {} },
      require(name) {
        if (name === "./lib/config") return {};
        if (name === "./lib/container-setup") return {
          setupContainer: async (options) => { calls.push(["container", options]); return { file: "/test/config", wrapper: "/test/wrapper", url: options.url }; }
        };
        if (name === "./lib/setup") return {
          setup: async (options) => { calls.push(["client", options]); return { configured: true }; }
        };
        throw new Error(`unexpected module ${name}`);
      }
    };
    vm.runInNewContext(source, context);
    await context.done;
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], container ? "container" : "client");
    assert.equal(calls[0][1].clientConfig, "/test/client.json");
    assert.equal(calls[0][1].url, "https://broker.test");
  }
});
