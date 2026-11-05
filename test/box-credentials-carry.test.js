// Folding a token a box refreshed back into the account's profile: which of
// the two records is newer, and what the merge is allowed to drop.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { temp, engine } = require("./helpers");

// The two writers of this file state the same instant in two shapes: a refresh
// out here writes the local offset, the harness inside a box writes UTC.
const inBox = {
  auth_method: "consumer",
  id_token_email: "person@example.com",
  token: { access_token: "fresh", refresh_token: "r2", token_type: "Bearer",
           expiry: "2026-11-05T18:31:00.402123095Z" },
};
const inProfile = {
  auth_method: "consumer",
  id_token: "header.payload.signature",
  token: { access_token: "older", refresh_token: "r1", token_type: "Bearer",
           expiry: "2026-11-05T18:45:53.906592+01:00" },
};

async function pair(dir) {
  const copy = path.join(dir, "auth-copy.json");
  const source = path.join(dir, "profile.json");
  await fs.writeFile(copy, JSON.stringify(inBox));
  await fs.writeFile(source, JSON.stringify(inProfile));
  return { copy, source };
}

test("an hour ahead in UTC beats a nearer instant with an offset", async (t) => {
  const dir = await temp(t);
  const { copy, source } = await pair(dir);

  const out = engine(`
from broker.box import harness
print(harness._expiry("${copy}") > harness._expiry("${source}"))
`);

  // 18:31Z is 19:31 where 18:45+01:00 is 17:45Z. Compared as text the first
  // one sorts lower, and the box's refreshed token was discarded for it.
  assert.match(out, /True/);
});

test("the carried record keeps a field the box's own record does not have", async (t) => {
  const dir = await temp(t);
  const { copy, source } = await pair(dir);

  engine(`
from broker.box import harness
harness._carry_credentials_back("${copy}", "${source}")
`);
  const held = JSON.parse(await fs.readFile(source, "utf8"));

  // The refreshed token arrives...
  assert.equal(held.token.access_token, "fresh");
  assert.equal(held.id_token_email, "person@example.com");
  // ...and id_token, which only the profile's own writer sets, stays. Copying
  // the file whole deleted it, so one box run cost the host account a field.
  assert.equal(held.id_token, "header.payload.signature");
});

test("a box holding an older token is not carried back", async (t) => {
  const dir = await temp(t);
  const copy = path.join(dir, "auth-copy.json");
  const source = path.join(dir, "profile.json");
  await fs.writeFile(copy, JSON.stringify({
    ...inBox, token: { ...inBox.token, expiry: "2026-11-05T10:00:00Z" },
  }));
  await fs.writeFile(source, JSON.stringify(inProfile));

  engine(`
from broker.box import harness
harness._carry_credentials_back("${copy}", "${source}")
`);
  const held = JSON.parse(await fs.readFile(source, "utf8"));

  assert.equal(held.token.access_token, "older");
  assert.equal(held.id_token_email, undefined);
});

test("an unreadable copy is left where it is", async (t) => {
  const dir = await temp(t);
  const copy = path.join(dir, "auth-copy.json");
  const source = path.join(dir, "profile.json");
  await fs.writeFile(copy, "half a record");
  await fs.writeFile(source, JSON.stringify(inProfile));

  engine(`
from broker.box import harness
harness._carry_credentials_back("${copy}", "${source}")
`);
  const held = JSON.parse(await fs.readFile(source, "utf8"));

  assert.equal(held.token.access_token, "older");
});
