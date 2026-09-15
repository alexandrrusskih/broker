const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { setup } = require("../lib/setup");
const { openPrompter } = require("../lib/prompt");
const { writeJson } = require("../functions/stores/files");

function fixture(saved = {}, env = {}, answers = []) {
  const writes = [];
  const questions = [];
  let closed = false;
  return {
    writes, questions, isClosed: () => closed,
    config: { FILE: "/test/config.json", read: (useEnv = true) => useEnv ? { ...env, ...saved } : { ...saved }, write: (v) => writes.push(v) },
    openPrompter: (noAsk) => noAsk ? null : {
      ask: async (...args) => { questions.push(args); return answers.shift(); },
      close: () => { closed = true; }
    }
  };
}

test("setup prompts for URL and hidden key without rewriting configured clients", async () => {
  const f = fixture({}, {}, ["y", "https://broker.example.test/", "fake-client-key"]);
  assert.equal((await setup({}, f)).changed, true);
  assert.deepEqual(f.writes, [{ url: "https://broker.example.test", key: "fake-client-key" }]);
  assert.deepEqual(f.questions[2][1], { secret: true });
  assert.equal(f.isClosed(), true);
  const existing = fixture({ url: "https://existing.test", key: "saved" });
  assert.equal((await setup({ url: "https://new.test" }, existing)).changed, false);
  assert.deepEqual(existing.writes, []);
  assert.deepEqual(existing.questions, []);
});

test("noninteractive/env-only setup never prompts or persists runtime secrets", async () => {
  assert.equal(openPrompter(true), null);
  const empty = fixture();
  assert.equal((await setup({ noAsk: true }, empty)).configured, false);
  assert.deepEqual(empty.writes, []);
  const runtime = fixture({}, { url: "https://runtime.test", key: "fake-env-key" });
  assert.equal((await setup({ noAsk: true }, runtime)).runtime, true);
  assert.deepEqual(runtime.writes, []);
  assert.deepEqual(runtime.questions, []);
  await assert.rejects(setup({ noAsk: true, url: "https://new.test" }, fixture()), /client key required/);
});

test("setup imports only client connection fields, not server admin credentials", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-setup-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const file = path.join(dir, "client.json");
  const f = fixture();
  await writeJson(file, { url: "https://broker.test", key: "fake", role: "client", accounts: { codex: "main" }, bin_dir: "/not-this-machine" });
  await setup({ clientConfig: file, noAsk: true }, f);
  assert.deepEqual(f.writes, [{ url: "https://broker.test", key: "fake", role: "client", accounts: { codex: "main" } }]);
  await writeJson(file, { url: "https://broker.test", key: "fake-admin", role: "admin" });
  await assert.rejects(setup({ clientConfig: file, noAsk: true }, fixture()), /not the server's admin/);
});
