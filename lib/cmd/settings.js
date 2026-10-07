// This machine's own config, and the few fields the broker itself keeps.
"use strict";

const config = require("../config");

function local(flags) {
  if (flags.url || flags.key || flags.account) {
    const patch = {};
    if (flags.url) patch.url = String(flags.url).replace(/\/$/, "");
    if (flags.key) patch.key = flags.key;
    if (flags.account) patch.account = flags.account;
    config.write(patch);
    console.log(`✓ config updated (${config.FILE})`);
  }
  const c = config.read();
  console.log(JSON.stringify({ url: c.url || null, key: c.key ? "<set>" : null, account: c.account || "default", project: c.project || null }, null, 2));
}

async function remoteSet(flags, positional) {
  // Set a whitelisted broker_config field (the codex rollout flag / handle
  // secret). This is the supported way to flip the rollout — see §6.8.
  const cfg = config.require();
  const key = positional[0];
  let value = positional[1];
  if (!key) throw new Error("usage: broker config-set codex_handle_rollout <true|false>\n       broker config-set codex_handle_secret <hex|generate>");
  if (key === "codex_handle_secret" && (value === "generate" || flags.generate)) {
    value = require("crypto").randomBytes(32).toString("hex");
    console.log("  (generated a random 32-byte secret; provision it BEFORE flipping the flag — see §6.8)");
  }
  const r = await fetch(`${cfg.url}/configSet`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-broker-key": cfg.key },
    body: JSON.stringify({ key, value })
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`configSet ${r.status}: ${body.error || "failed"}${body.settable ? " (settable: " + body.settable.join(", ") + ")" : ""}`);
  console.log(`✓ ${key} = ${body.value}`);
  if (key === "codex_handle_rollout" && body.value === true) {
    console.log("  ⚠ handles are now issued — make sure every cx/CI is updated and NO long codex session is live (§3 collision).");
  }
}

async function remoteGet() {
  const cfg = config.require();
  const r = await fetch(`${cfg.url}/configGet`, { headers: { "x-broker-key": cfg.key } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`configGet ${r.status}: ${body.error || "failed"}`);
  console.log(`codex_handle_rollout:    ${body.codex_handle_rollout}`);
  console.log(`codex_handle_secret_set: ${body.codex_handle_secret_set}`);
}

module.exports = { local, remoteSet, remoteGet };
