// Handing the broker a token, and asking it for one.
"use strict";

const config = require("../config");

async function seed(flags, positional) {
  const { seed } = require("../seed");
  const provider = positional[0];
  if (!provider) throw new Error("usage: broker seed <codex|claude|agy|glm> [--account <name>]");
  const account = flags.account || config.read().account;
  if (!account) throw new Error(`which account? pass --account <name> (or set one with 'broker set-default')`);
  await seed(provider, account);
  console.log(`✓ seeded ${provider} (account: ${account}) — broker refresh confirmed (200)`);
}

async function get(flags, positional) {
  const { get } = require("../get");
  const provider = positional[0];
  if (!provider) throw new Error("usage: broker get <provider> [--format authjson|raw] [--account <name>]");
  const account = flags.account || config.read().account;
  if (!account) throw new Error(`which account? pass --account <name> (or set one with 'broker set-default')`);
  const body = await get(provider, flags.format || "raw", account);
  process.stdout.write(body.endsWith("\n") ? body : body + "\n");
}

module.exports = { seed, get };
