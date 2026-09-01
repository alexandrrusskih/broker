const fs = require("fs");
const os = require("os");
const path = require("path");

const DIR = path.join(os.homedir(), ".config", "hltm-broker");
const FILE = path.join(DIR, "config.json");

// There is deliberately no shared default deployment. Every installation must
// name its own broker URL in local config or BROKER_URL.
const DEFAULT_URL = process.env.BROKER_URL || null;

// A url saved before the single-function move points at the per-function base,
// where every action now 404s. Nudge it onto the router rather than making
// everyone re-run `broker config --url`.
function withRouter(url) {
  return /cloudfunctions\.net\/?$/.test(url) ? url.replace(/\/+$/, "") + "/broker" : url;
}

function read() {
  let cfg = {};
  try {
    cfg = JSON.parse(fs.readFileSync(FILE, "utf8"));
  } catch (_e) {
    cfg = {};
  }
  cfg.url = cfg.url ? withRouter(cfg.url) : DEFAULT_URL;
  // The gemini provider became agy — gemini-cli was a package nobody called,
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
  fs.mkdirSync(DIR, { recursive: true });
  const next = { ...read(), ...patch };
  fs.writeFileSync(FILE, JSON.stringify(next, null, 2), { mode: 0o600 });
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
  read, write, require: require_, accountFor, setAccountFor, FILE, DIR, DEFAULT_URL
};
