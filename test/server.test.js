const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { init, serve } = require("../lib/server");
const { createStore } = require("../functions/store");
const { createFileAdapter, readJson } = require("../functions/stores/files");
const { createRouter } = require("../functions/router");
const codex = require("../functions/providers/codex");
const quiet = { log() {}, error() {} };

test("self-hosted HTTP: admin/client separation, opaque handles, rotation and restart", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-http-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const out = await init({ dataDir: dir, url: "https://example.test" });
  const client = await readJson(out.clientFile);
  const admin = await readJson(out.adminFile);
  assert.notEqual(client.key, admin.key);
  assert.equal((await fs.stat(out.adminFile)).mode & 0o777, 0o600);
  await assert.rejects(init({ dataDir: dir }), /already initialized/);
  assert.deepEqual(await readJson(out.clientFile), client);
  let server = await serve({ dataDir: dir, port: 0, logger: quiet });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const call = async (action, { key = client.key, body, form, method } = {}) => {
    const response = await fetch(`http://127.0.0.1:${server.address().port}/${action}`, {
      method: method || (body || form ? "POST" : "GET"),
      headers: { "x-broker-key": key, "Content-Type": form ? "application/x-www-form-urlencoded" : "application/json" },
      body: form ? new URLSearchParams(form) : body ? JSON.stringify(body) : undefined
    });
    assert.equal(response.headers.get("cache-control"), "no-store");
    return { status: response.status, body: await response.json() };
  };
  const seed = { provider: "codex", account: "main", refresh_token: "REAL-SERVER-ONLY", client_id: "test", expires_at: 0 };
  assert.equal((await call("listAccounts?provider=codex", { key: "wrong" })).status, 401);
  assert.equal((await call("seedToken", { body: seed })).status, 401);
  assert.equal((await call("seedToken", { key: admin.key, body: seed })).status, 200);
  assert.equal((await call("bootstrap", { body: {} })).status, 404);
  assert.equal((await call("toString")).status, 404);
  assert.equal((await call("getToken?provider=__proto__")).status, 400);

  let rotations = 0;
  t.mock.method(codex, "refresh", async () => {
    rotations++;
    await new Promise((r) => setTimeout(r, 50));
    return { access_token: `fake-access-${rotations}`, refresh_token: `REAL-ROTATED-${rotations}`, expires_at: Date.now() + 3_600_000 };
  });
  const results = await Promise.all(Array.from({ length: 20 }, () => call("getToken?provider=codex&account=main&format=authjson")));
  assert.ok(results.every((r) => r.status === 200));
  assert.equal(rotations, 1);
  const handle = results[0].body.tokens.refresh_token;
  assert.match(handle, /^[0-9a-f]{64}$/);
  assert.ok(!JSON.stringify(results).includes("REAL-"));
  assert.deepEqual((await call("listAccounts?provider=codex")).body.accounts, ["main"]);
  assert.equal((await call("deleteAccount?provider=codex&account=main", { method: "POST" })).status, 401);
  assert.equal((await call("configSet", { body: { key: "codex_handle_rollout", value: false } })).status, 401);
  assert.equal((await call("configSet", { key: admin.key, body: { key: "codex_handle_rollout", value: false } })).status, 400);
  assert.equal((await call("oauthRefresh?account=main", { key: "", form: { refresh_token: "wrong" } })).status, 401);
  const refreshed = await call("oauthRefresh?account=main", { key: "", form: { refresh_token: handle, grant_type: "refresh_token" } });
  assert.equal(refreshed.status, 200);
  assert.equal(refreshed.body.refresh_token, handle);
  assert.equal(rotations, 2);
  assert.equal((await call("oauthRevoke?account=main", { form: { token: handle } })).status, 200);

  await new Promise((resolve) => server.close(resolve));
  server = await serve({ dataDir: dir, port: 0, logger: quiet });
  const after = await call("getToken?provider=codex&account=main&format=authjson");
  assert.equal(after.body.tokens.refresh_token, handle);
  assert.equal(after.body.tokens.access_token, "fake-access-2");
  assert.equal(rotations, 2);
  assert.equal((await call("deleteAccount?provider=codex&account=main", { key: admin.key, method: "POST" })).status, 200);
  assert.equal((await call("getToken?provider=codex&account=main")).status, 404);
  assert.equal((await call("oauthRefresh?account=main", { form: { refresh_token: handle } })).status, 401);
});

test("Firebase router keeps legacy key permissions and rollout default", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-legacy-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const store = createStore(createFileAdapter(dir));
  await store.claimConfig({ broker_key: "legacy-key" });
  const route = createRouter(store, { logger: quiet });
  const invoke = async (action, query = {}, body) => {
    const req = { path: `/${action}`, query, body, method: body ? "POST" : "GET", get: () => "legacy-key" };
    const res = { status(code) { this.code = code; return this; }, json(value) { this.body = value; } };
    await route(req, res);
    return res;
  };
  assert.equal((await invoke("seedToken", {}, { provider: "codex", refresh_token: "legacy-refresh", access_token: "fake", expires_at: Date.now() + 3_600_000 })).code, 200);
  assert.equal((await invoke("getToken", { provider: "codex", format: "authjson" })).body.tokens.refresh_token, "legacy-refresh");
  assert.equal((await invoke("configSet", {}, { key: "codex_handle_rollout", value: true })).code, 200);
  assert.match((await invoke("getToken", { provider: "codex", format: "authjson" })).body.tokens.refresh_token, /^[0-9a-f]{64}$/);
});
