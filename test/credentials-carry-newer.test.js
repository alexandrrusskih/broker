// Which of two id_tokens the profile is left holding: the copy the broker
// stored when it was seeded, or the one a fresh login has just written. Only a
// login mints one, so the broker's can be the older of the two.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { temp, engine } = require("./helpers");

// Read for its `exp` and nothing else, so the header and the signature here can
// be any text at all.
function jwt(exp) {
  const body = Buffer.from(JSON.stringify({ exp })).toString("base64url");
  return `header.${body}.signature`;
}

const EARLIER = jwt(1700000000);
const LATER = jwt(1700003600);

async function carry(dir, onDisk, fromBroker) {
  const target = path.join(dir, "auth.json");
  const sent = path.join(dir, "sent.json");
  await fs.writeFile(target, JSON.stringify(onDisk));
  await fs.writeFile(sent, JSON.stringify(fromBroker));
  engine(`
import json, types
from broker import credentials
provider = types.SimpleNamespace(AUTH_KEEP=("id_token",))
with open("${sent}") as fh:
    auth = json.load(fh)
held = credentials._keep_what_the_broker_cannot_send(provider, "${target}", auth)
with open("${target}", "w") as fh:
    json.dump(held, fh)
`);
  return JSON.parse(await fs.readFile(target, "utf8"));
}

test("a login's newer id_token survives the broker's stored copy", async (t) => {
  const dir = await temp(t);
  const held = await carry(dir, { id_token: LATER }, { id_token: EARLIER });

  // Measured before this rule existed: a sign-in at 19:54, a run at 19:56, and
  // the id_token from 19:13 back in the file — the login undone, with the
  // harness asking for another one and no way to make it stop.
  assert.equal(held.id_token, LATER);
});

test("the broker's answer wins when the file holds the staler token", async (t) => {
  const dir = await temp(t);
  const held = await carry(dir, { id_token: EARLIER }, { id_token: LATER });

  assert.equal(held.id_token, LATER);
});

test("a field the broker does not send at all is still carried forward", async (t) => {
  const dir = await temp(t);
  const held = await carry(dir, { id_token: EARLIER }, { token: { access_token: "fresh" } });

  // The original reason for carrying anything: a refresh usually returns no
  // id_token, and agy will not start without one.
  assert.equal(held.id_token, EARLIER);
  assert.equal(held.token.access_token, "fresh");
});

test("neither copy being a token leaves the broker's value in place", async (t) => {
  const dir = await temp(t);
  const held = await carry(dir, { id_token: "not-a-jwt" }, { id_token: "from-the-broker" });

  // No expiry to compare, so the rule this file is about does not apply and the
  // behaviour is what it always was.
  assert.equal(held.id_token, "from-the-broker");
});
