const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

// Where the broker keeps its things on this machine. Declared once here and
// imported everywhere else — the Python engine does the same in
// lib/wrappers/broker/config.py, and the two must agree.
const BROKER_REL = ".config/broker";
const CONFIG_REL = `${BROKER_REL}/config.json`;
// The engine caches credentials beside the config, and always under the real
// home: $BROKER_CONFIG selects an installation, not a cache directory.
const CACHE_DIR = path.join(os.homedir(), ...BROKER_REL.split("/"), "cache");
const FILE = process.env.BROKER_CONFIG
  ? path.resolve(process.env.BROKER_CONFIG)
  : path.join(os.homedir(), ...CONFIG_REL.split("/"));
const DIR = path.dirname(FILE);

// DROP AFTER 2026-12. The tool was called hltm-broker until September 2026; an
// install from back then is read from the old file and copied across on the
// first write, so nobody has to re-run `broker config` after an update.
const LEGACY_FILE = path.join(os.homedir(), ".config", "hltm-broker", "config.json");

function sourceFile() {
  if (process.env.BROKER_CONFIG || fs.existsSync(FILE)) return FILE;
  return fs.existsSync(LEGACY_FILE) ? LEGACY_FILE : FILE;
}

// There is deliberately no shared default deployment. Every installation must
// name its own broker URL in local config or BROKER_URL.
const DEFAULT_URL = process.env.BROKER_URL || null;

// A url saved before the single-function move points at the per-function base,
// where every action now 404s. Nudge it onto the router rather than making
// everyone re-run `broker config --url`.
function withRouter(url) {
  return /cloudfunctions\.net\/?$/.test(url) ? url.replace(/\/+$/, "") + "/broker" : url;
}

function read(useEnv = true) {
  let cfg = {};
  try {
    cfg = JSON.parse(fs.readFileSync(sourceFile(), "utf8"));
  } catch (_e) {
    cfg = {};
  }
  // Preserve the existing installation's precedence. Environment values fill
  // missing fields; BROKER_CONFIG selects a different installation explicitly.
  cfg.url = withRouter(cfg.url || (useEnv && process.env.BROKER_URL) || null);
  if (!cfg.key && useEnv && process.env.BROKER_KEY) cfg.key = process.env.BROKER_KEY;
  // DROP AFTER 2026-12. The gemini provider became agy — gemini-cli was a package nobody called,
  // while agy is the harness that actually runs. Carry an old machine's settings
  // across instead of silently forgetting its account and its shim.
  if (cfg.accounts && cfg.accounts.gemini && !cfg.accounts.agy) {
    cfg.accounts = { ...cfg.accounts, agy: cfg.accounts.gemini };
  }
  if (Array.isArray(cfg.shims) && cfg.shims.includes("gemini") && !cfg.shims.includes("agy")) {
    cfg.shims = cfg.shims.map((name) => (name === "gemini" ? "agy" : name));
  }
  return cfg;
}

function write(patch) {
  fs.mkdirSync(DIR, { recursive: true, mode: 0o700 });
  // Runtime overrides must not get baked into image builds or overwrite a
  // saved admin/client credential when only changing the default account.
  const next = { ...read(false), ...patch };
  const temp = path.join(DIR, `.config-${crypto.randomUUID()}`);
  try {
    fs.writeFileSync(temp, JSON.stringify(next, null, 2), { mode: 0o600, flag: "wx" });
    fs.renameSync(temp, FILE);
  } finally { fs.rmSync(temp, { force: true }); }
  return next;
}

function require_(cfg) {
  const c = cfg || read();
  if (!c.url) {
    throw new Error("broker not configured — run `broker config --url <broker-url> --key <broker_key>`");
  }
  if (!c.key) {
    throw new Error("broker not configured — run `broker config --url <broker-url> --key <broker_key>`");
  }
  return c;
}

// Which account this machine belongs to for a given provider. Accounts differ
// per provider (your codex is not your claude), so the per-provider map wins;
// the single `account` stays as the fallback for setups written before it.
function accountFor(provider, cfg) {
  const c = cfg || read();
  return (c.accounts || {})[provider] || c.account || null;
}

function setAccountFor(provider, account) {
  const accounts = { ...(read().accounts || {}), [provider]: account };
  write({ accounts });
  return account;
}

module.exports = {
  read, write, require: require_, accountFor, setAccountFor,
  FILE, DIR, DEFAULT_URL, CONFIG_REL, CACHE_DIR
};
