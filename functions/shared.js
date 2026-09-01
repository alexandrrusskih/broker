const crypto = require("crypto");

const store = require("./store");
const { secretMatches } = require("./constant-time");

// Rotating tokens live in Firestore. The one-time bootstrap credential lives in
// Secret Manager and is bound only to the broker function.
const providers = {
  codex: require("./providers/codex"),
  claude: require("./providers/claude"),
  gemini: require("./providers/gemini"),
  agy: require("./providers/agy"),
  glm: require("./providers/glm")
};

function secretsFor(cfg, provider) {
  if (provider === "gemini") {
    return { client_id: cfg.gemini_client_id, client_secret: cfg.gemini_client_secret };
  }
  // agy carries working defaults in its own module; config only overrides them.
  if (provider === "agy") {
    return { client_id: cfg.agy_client_id, client_secret: cfg.agy_client_secret };
  }
  // codex/claude carry client_id in the seeded token; no client_secret needed.
  return {};
}

// HMAC key for codex handles. A dedicated `codex_handle_secret` in broker_config
// is preferred; absent one, derive it from broker_key with domain separation so
// the bearer key is never used directly as HMAC key material (the handle is
// one-way regardless, but this keeps the two rotatable independently).
function handleSecret(cfg) {
  if (cfg.codex_handle_secret) return cfg.codex_handle_secret;
  return crypto.createHmac("sha256", cfg.broker_key || "").update("codex-handle-v1").digest();
}

function codexHandle(cfg, account) {
  return providers.codex.refreshHandle(handleSecret(cfg), account);
}

// Constant-time compare of a presented handle against the expected one.
function handleMatches(presented, expected) {
  const a = Buffer.from(String(presented || ""));
  const b = Buffer.from(String(expected || ""));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

async function authorized(req) {
  const cfg = await store.readConfig();
  return secretMatches(req.get("x-broker-key"), cfg.broker_key);
}

// A dead grant needs a manual local re-login + re-seed; a blip does not. Specific
// terms only — a bare `refresh`/`expired` catch-all misfires on transient
// "codex refresh failed 503: …" and pages the operator on nothing.
const DEAD_GRANT =
  /invalid_grant|reuse|refresh_token_(expired|invalidated)|\b401\b|rotation_pending|re-seed required/i;

function isDeadGrant(message) {
  return DEAD_GRANT.test(message || "");
}

// Optional alert when a refresh token dies (needs re-auth). Self-contained: if
// `alert_webhook` is set in broker_config, POST {text} to it (Slack-incoming or
// any JSON webhook); otherwise stay silent. No external module dependency.
async function alertReauth(cfg, message) {
  const url = cfg.alert_webhook;
  if (!url) return;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: message, message })
    });
  } catch (_e) {
    // best-effort
  }
}

// Alert at most once per account per hour, then report the outcome to the caller.
async function alertOnce(provider, account, message) {
  const cfg = await store.readConfig();
  if (await store.shouldAlert(provider, account)) {
    await alertReauth(cfg, message);
  }
}

module.exports = {
  providers,
  secretsFor,
  handleSecret,
  codexHandle,
  handleMatches,
  authorized,
  isDeadGrant,
  alertReauth,
  alertOnce
};
