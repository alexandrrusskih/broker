const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { createStore } = require("../functions/store");
const { createFileAdapter } = require("../functions/stores/files");
const { createFirestoreAdapter } = require("../functions/stores/firestore");

const seeded = { refresh_token: "fake-refresh-old", access_token: "fake-access-old", expires_at: 0 };
const fresh = () => ({ refresh_token: "fake-refresh-new", access_token: "fake-access-new", expires_at: Date.now() + 3_600_000 });
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };

// Only the Firestore primitives the adapter uses. Transactions serialize and
// commits are atomic; this is a contract test, not a live Firebase integration.
function fakeFirestore() {
  const documents = new Map();
  let queue = Promise.resolve();
  const snapshot = (key) => ({ exists: documents.has(key), data: () => structuredClone(documents.get(key)) });
  return {
    collection(name) {
      return {
        doc: (id) => ({ key: `${name}/${id}`, get: async () => snapshot(`${name}/${id}`) }),
        select: () => ({ get: async () => ({ docs: [...documents].filter(([k]) => k.startsWith(`${name}/`))
          .map(([k, v]) => ({ id: k.slice(name.length + 1), get: (field) => v[field] })) }) })
      };
    },
    runTransaction(fn) {
      const result = queue.catch(() => {}).then(async () => {
        const writes = [];
        const value = await fn({
          get: async (ref) => snapshot(ref.key),
          set: (ref, data) => writes.push(() => documents.set(ref.key, structuredClone(data))),
          delete: (ref) => writes.push(() => documents.delete(ref.key))
        });
        writes.forEach((write) => write());
        return value;
      });
      queue = result;
      return result;
    }
  };
}

async function fixture(t, backend) {
  if (backend === "firestore") {
    const db = fakeFirestore();
    return () => createStore(createFirestoreAdapter(db));
  }
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-store-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  return () => createStore(createFileAdapter(dir));
}

for (const backend of ["files", "firestore"]) {
  test(`${backend}: parallel get/forced refresh coalesce to one rotation`, async (t) => {
    const open = await fixture(t, backend);
    const store = open();
    for (const force of [false, true]) {
      await store.write("codex", "main", { ...seeded, expires_at: force ? Date.now() + 3_600_000 : 0 });
      let calls = 0;
      const results = await Promise.all(Array.from({ length: 20 }, () => store.withRefreshLease("codex", "main", async () => {
        calls++;
        await new Promise((r) => setTimeout(r, 20));
        return fresh();
      }, force ? { coalesceFrom: seeded.access_token } : {})));
      assert.equal(calls, 1);
      assert.ok(results.every((r) => r.access_token === "fake-access-new"));
      assert.equal((await open().read("codex", "main")).rotation_pending, false);
    }
  });

  test(`${backend}: ambiguous failure survives restart and requires re-seed`, async (t) => {
    const open = await fixture(t, backend);
    const store = open();
    await store.write("codex", "main", seeded);
    await assert.rejects(store.withRefreshLease("codex", "main", async () => { throw new Error("timeout"); }), /timeout/);
    const restarted = open();
    assert.equal((await restarted.read("codex", "main")).rotation_pending, true);
    await assert.rejects(restarted.withRefreshLease("codex", "main", () => assert.fail("must not replay")), /rotation_pending/);
    await restarted.write("codex", "main", { ...seeded, rotation_pending: false, lease_owner: null, lease_until: 0 });
    assert.equal((await restarted.withRefreshLease("codex", "main", async () => fresh())).access_token, "fake-access-new");
  });

  test(`${backend}: an expired crash journal never permits replay`, async (t) => {
    const open = await fixture(t, backend);
    await open().write("codex", "main", { ...seeded, rotation_pending: true, lease_until: 1, lease_owner: "dead-process" });
    await assert.rejects(open().withRefreshLease("codex", "main", () => assert.fail("must not replay")), /rotation_pending/);
  });

  for (const fails of [false, true]) {
    for (const change of ["seed", "delete"]) {
      test(`${backend}: ${change} fences an in-flight ${fails ? "failed" : "successful"} rotation`, async (t) => {
        const open = await fixture(t, backend);
        const store = open();
        await store.write("codex", "main", seeded);
        const entered = deferred();
        const finish = deferred();
        const rotating = store.withRefreshLease("codex", "main", async () => {
          entered.resolve();
          await finish.promise;
          if (fails) throw new Error("timeout");
          return fresh();
        });
        const rejected = assert.rejects(rotating, fails ? /timeout/ : /lease lost/);
        await entered.promise;
        if (change === "delete") await store.remove("codex", "main");
        else await store.write("codex", "main", { ...seeded, refresh_token: "reseed", lease_owner: "new-owner", rotation_pending: false });
        finish.resolve();
        await rejected;
        const result = await store.read("codex", "main");
        if (change === "delete") assert.equal(result, null);
        else { assert.equal(result.refresh_token, "reseed"); assert.equal(result.lease_owner, "new-owner"); }
      });
    }
  }

  test(`${backend}: no stale token/config cache, no deleted-account resurrection`, async (t) => {
    const open = await fixture(t, backend);
    const a = open();
    const b = open();
    await a.write("codex", "main", seeded);
    await a.read("codex", "main");
    await b.write("codex", "main", fresh());
    assert.equal((await a.read("codex", "main")).refresh_token, "fake-refresh-new");
    await b.remove("codex", "main");
    assert.equal(await a.read("codex", "main"), null);
    assert.equal(await a.shouldAlert("codex", "main"), false);
    await assert.rejects(a.withRefreshLease("codex", "main", () => assert.fail("deleted")), /not seeded/);
    assert.deepEqual(await a.listAccounts("codex"), []);
    assert.equal(await a.claimConfig({ broker_key: "first" }), true);
    assert.equal(await b.claimConfig({ broker_key: "second" }), false);
    await b.writeConfig({ codex_handle_rollout: true });
    assert.equal((await a.readConfig()).codex_handle_rollout, true);
    assert.equal((await a.readConfig()).broker_key, "first");
  });

  test(`${backend}: a slow account does not block another account`, async (t) => {
    const open = await fixture(t, backend);
    const store = open();
    await store.write("codex", "slow", seeded);
    await store.write("codex", "fast", seeded);
    const entered = deferred();
    const finish = deferred();
    const slow = store.withRefreshLease("codex", "slow", async () => { entered.resolve(); await finish.promise; return fresh(); });
    await entered.promise;
    try { await store.withRefreshLease("codex", "fast", async () => fresh()); }
    finally { finish.resolve(); }
    await slow;
  });
}

test("files: safe filenames, private modes and atomic reads during writes", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-files-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const store = createStore(createFileAdapter(dir));
  const account = "../../main / человек";
  await store.write("codex", account, seeded);
  assert.deepEqual(await store.listAccounts("codex"), [account]);
  const reads = Array.from({ length: 30 }, async () => {
    const value = await store.read("codex", account);
    assert.ok(value.refresh_token);
  });
  await Promise.all([...reads, ...Array.from({ length: 20 }, () => store.write("codex", account, fresh()))]);
  const accountsDir = path.join(dir, "accounts", "_codex");
  const files = await fs.readdir(accountsDir);
  assert.equal(files.length, 1);
  assert.equal((await fs.stat(path.join(accountsDir, files[0]))).mode & 0o777, 0o600);
  assert.equal((await fs.stat(accountsDir)).mode & 0o777, 0o700);
});
