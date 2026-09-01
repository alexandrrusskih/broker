const { log } = require("firebase-functions/logger");
const admin = require("firebase-admin");
const crypto = require("crypto");

const store = require("./store");
const { authorized } = require("./shared");
const { secretMatches } = require("./constant-time");
const { claimBootstrap } = require("./bootstrap-claim");

const settingsRef = () => admin.firestore().collection("broker_config").doc("_settings");

// POST /bootstrap — one-time and protected by a random Secret Manager value
// provisioned by the deploy command. The transaction makes concurrent claims
// deterministic: exactly one caller can create and receive the broker key.
async function bootstrap(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "use POST" });
  if (!secretMatches(req.get("x-bootstrap-token"), process.env.BROKER_BOOTSTRAP_TOKEN)) {
    return res.status(401).json({ error: "unauthorized" });
  }

  const brokerKey = crypto.randomBytes(32).toString("hex");
  const extra = {};
  if (req.body && typeof req.body.alert_webhook === "string") {
    let webhook;
    try {
      webhook = new URL(req.body.alert_webhook);
    } catch (_err) {
      return res.status(400).json({ error: "alert_webhook must be a valid https URL" });
    }
    if (webhook.protocol !== "https:") {
      return res.status(400).json({ error: "alert_webhook must use https" });
    }
    extra.alert_webhook = webhook.toString();
  }

  const claimed = await claimBootstrap(admin.firestore(), settingsRef(), {
    broker_key: brokerKey,
    created_at: Date.now(),
    ...extra
  });
  if (!claimed) return res.status(403).json({ error: "already_bootstrapped" });

  log("bootstrapped broker_config");
  return res.status(200).json({ broker_key: brokerKey });
}

// Fields that /configSet may write. broker_key and provider creds are NOT here —
// bootstrap owns those, and this endpoint must never overwrite the auth key. This
// is how the codex handle rollout is flipped and its secret provisioned without a
// Firestore console (see docs/codex-refresh-authority.md §6.8).
const CONFIG_SETTABLE = { codex_handle_rollout: "boolean", codex_handle_secret: "string" };

// POST /configSet { key, value } — set one whitelisted broker_config field. broker-key gated.
async function configSet(req, res) {
  if (!(await authorized(req))) return res.status(401).json({ error: "unauthorized" });
  if (req.method !== "POST") return res.status(405).json({ error: "use POST" });

  const key = String((req.body && req.body.key) || "");
  const type = CONFIG_SETTABLE[key];
  if (!type) {
    return res.status(400).json({ error: `not settable: ${key}`, settable: Object.keys(CONFIG_SETTABLE) });
  }

  let value = req.body ? req.body.value : undefined;
  if (type === "boolean") {
    value = value === true || value === "true" || value === "on" || value === 1 || value === "1";
  } else if (typeof value !== "string" || !value) {
    return res.status(400).json({ error: `${key} must be a non-empty string` });
  }

  await settingsRef().set({ [key]: value }, { merge: true });
  // Never log a secret value.
  log(`config-set ${key} = ${type === "boolean" ? value : "<redacted>"}`);
  return res.status(200).json({ ok: true, key, value: type === "boolean" ? value : "<set>" });
}

// GET /configGet — the rollout state. NEVER returns secret values, only booleans,
// so an operator can verify a flip/provision without exposing anything.
async function configGet(req, res) {
  if (!(await authorized(req))) return res.status(401).json({ error: "unauthorized" });
  const cfg = await store.readConfig();
  return res.status(200).json({
    codex_handle_rollout: cfg.codex_handle_rollout === true,
    codex_handle_secret_set: Boolean(cfg.codex_handle_secret)
  });
}

module.exports = { bootstrap, configSet, configGet };
