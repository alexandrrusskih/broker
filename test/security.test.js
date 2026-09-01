"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { secretMatches } = require("../functions/constant-time");
const { claimBootstrap } = require("../functions/bootstrap-claim");
const { deploy } = require("../lib/deploy");
const agy = require("../functions/providers/agy");

test("secret comparison fails closed", () => {
  assert.equal(secretMatches(undefined, "expected"), false);
  assert.equal(secretMatches("presented", undefined), false);
  assert.equal(secretMatches("wrong", "expected"), false);
  assert.equal(secretMatches("expected", "expected"), true);
});

test("bootstrap claim is atomic and refuses an existing key", async () => {
  const writes = [];
  const db = {
    runTransaction: async (callback) => callback({
      get: async () => ({ exists: true, data: () => ({ broker_key: "already-set" }) }),
      set: (...args) => writes.push(args)
    })
  };
  assert.equal(await claimBootstrap(db, "settings-ref", { broker_key: "new" }), false);
  assert.deepEqual(writes, []);
});

test("bootstrap claim creates the key inside its transaction", async () => {
  const writes = [];
  const db = {
    runTransaction: async (callback) => callback({
      get: async () => ({ exists: false, data: () => undefined }),
      set: (...args) => writes.push(args)
    })
  };
  const payload = { broker_key: "new" };
  assert.equal(await claimBootstrap(db, "settings-ref", payload), true);
  assert.deepEqual(writes, [["settings-ref", payload, { merge: true }]]);
});

test("deploy refuses a shared Firebase project before external work", async () => {
  await assert.rejects(
    deploy({ project: "shared-project", dedicatedProject: false }),
    /dedicated Firebase project/
  );
});

test("agy refresh requires explicitly supplied OAuth client credentials", async () => {
  await assert.rejects(
    agy.refresh({ refresh_token: "placeholder" }, {}),
    /missing OAuth client credentials/
  );
});
