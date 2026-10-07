// Which accounts the broker holds for a provider, and letting one go.
"use strict";

const config = require("../config");

async function list(positional) {
  const cfg = config.require();
  const provider = positional[0];
  if (!provider) throw new Error("usage: broker accounts <codex|claude|agy|glm>");
  const r = await fetch(`${cfg.url}/listAccounts?provider=${encodeURIComponent(provider)}`, {
    headers: { "x-broker-key": cfg.key }
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`listAccounts ${r.status}: ${body.error || "failed"}`);
  const current = cfg.account || "default";
  const accounts = body.accounts || [];
  if (!accounts.length) {
    console.log(`no ${provider} accounts seeded — run 'broker seed ${provider} --account <name>'`);
    return;
  }
  for (const a of accounts) console.log(a === current ? `* ${a}` : `  ${a}`);
}

async function forget(flags, positional) {
  const cfg = config.require();
  const provider = positional[0];
  const account = flags.account || positional[1];
  if (!provider || !account || account === true) {
    throw new Error("usage: broker forget <provider> --account <name>");
  }
  if (!flags.yes) throw new Error(`refusing without --yes: this drops ${provider}/${account}'s refresh token for good`);
  const r = await fetch(
    `${cfg.url}/deleteAccount?provider=${encodeURIComponent(provider)}&account=${encodeURIComponent(account)}`,
    { method: "POST", headers: { "x-broker-key": cfg.key } }
  );
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`deleteAccount ${r.status}: ${body.error || "failed"}`);
  console.log(body.existed ? `✓ ${provider}/${account} deleted` : `${provider}/${account} was not there`);
  // Deleting the account in the broker used to leave its token on this
  // machine — and for agy that is the real refresh token, so "deleted" meant
  // deleted in one place only. History stays; credentials do not.
  for (const gone of dropCredentials(provider, account)) {
    console.log(`  removed its credentials: ${gone}`);
  }
}

function setDefault(flags, positional) {
  const account = positional[0] || flags.account;
  if (!account || account === true) throw new Error("usage: broker set-default <account>");
  const before = config.read().account || "default";
  config.write({ account: String(account) });
  console.log(`✓ default account: ${before} -> ${account} (${config.FILE})`);
  console.log(`  broker-cx and broker-agy pick per run; broker-cl follows this default`);
}

// Where an account's credentials sit on this machine, for the two shapes that
function dropCredentials(provider, account) {
// exist: a file inside a per-account profile, and the wrapper's own cache.
function dropCredentials(provider, account) {
  const fsMod = require("fs");
  const pathMod = require("path");
  const osMod = require("os");
  const { WRAP } = require("../wrap");
  const w = WRAP[provider];
  const paths = [
    pathMod.join(config.CACHE_DIR, `${provider}-${account}.json`)
  ];
  if (w && w.profileBase && w.authRel) {
    paths.push(pathMod.join(osMod.homedir(), `${w.profileBase}-${account}`, w.authRel));
  }
  const removed = [];
  for (const target of paths) {
    try {
      if (!fsMod.existsSync(target)) continue;
      fsMod.rmSync(target);
      removed.push(target);
    } catch (_e) {
      // a credential we cannot remove is worth saying nothing about here; the
      // account is already gone from the broker either way
    }
  }
  return removed;
}
}

module.exports = { list, forget, setDefault, dropCredentials };
