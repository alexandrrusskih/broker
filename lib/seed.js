const fs = require("fs");
const os = require("os");
const path = require("path");
const config = require("./config");

const HOME = os.homedir();

// Honour the CLI's own config-dir variable, so a second account can be logged
// in inside its own profile (CODEX_HOME=~/.codex-account-b codex login) without the
// login overwriting the auth file of whoever owns the canonical home.
function authDir(envVar, fallback) {
  const dir = envVar && process.env[envVar];
  return dir ? path.resolve(dir.replace(/^~(?=$|\/)/, HOME)) : path.join(HOME, fallback);
}

// Each loader reads the provider's LOCAL auth file (the one its CLI writes after
// login) and returns the fields the broker needs to own the refresh from now on.
const LOADERS = {
  codex() {
    const a = JSON.parse(fs.readFileSync(path.join(authDir("CODEX_HOME", ".codex"), "auth.json"), "utf8"));
    const t = a.tokens || {};
    return {
      provider: "codex",
      refresh_token: t.refresh_token,
      client_id: "app_EMoamEEZ73f0CkXaXp7hrann",
      account_id: t.account_id,
      id_token: t.id_token,
      expires_at: 0
    };
  },
  claude() {
    // A setup-token (sk-ant-oat01-…) is issued for a year, does not rotate and is
    // used verbatim — so the broker keeps it rather than refreshing it, and no
    // second holder can invalidate it. `broker-cl auth` puts it here.
    const oat = process.env.CLAUDE_CODE_OAUTH_TOKEN;
    if (oat) {
      return {
        provider: "claude",
        api_key: oat,
        access_token: oat,
        refresh_token: oat, // satisfies seedToken's required-field check
        expires_at: Date.now() + 365 * 24 * 3600 * 1000,
        subscription_type: "max"
      };
    }
    const a = JSON.parse(
      fs.readFileSync(path.join(authDir("CLAUDE_CONFIG_DIR", ".claude"), ".credentials.json"), "utf8")
    );
    const o = a.claudeAiOauth || {};
    return {
      provider: "claude",
      refresh_token: o.refreshToken,
      access_token: o.accessToken,
      expires_at: 0,
      scope: (o.scopes || []).join(" "),
      subscription_type: o.subscriptionType || "max",
      client_id: process.env.CLAUDE_OAUTH_CLIENT_ID
    };
  },
  gemini() {
    const a = JSON.parse(fs.readFileSync(path.join(HOME, ".gemini/oauth_creds.json"), "utf8"));
    return {
      provider: "gemini",
      refresh_token: a.refresh_token,
      access_token: a.access_token,
      scope: a.scope,
      expires_at: 0
    };
  },
  agy() {
    // agy's profile is a home directory, so the account being seeded is the one
    // whose HOME this process was handed (broker-agy auth sets it); HLTM_AGY_HOME
    // overrides it for a seed run outside that flow.
    const home = process.env.HLTM_AGY_HOME
      ? path.resolve(process.env.HLTM_AGY_HOME.replace(/^~(?=$|\/)/, HOME))
      : HOME;
    const file = path.join(home, ".gemini", "antigravity-cli", "antigravity-oauth-token");
    const a = JSON.parse(fs.readFileSync(file, "utf8"));
    const t = a.token || {};
    return {
      provider: "agy",
      refresh_token: t.refresh_token,
      access_token: t.access_token,
      token_type: t.token_type || "Bearer",
      auth_method: a.auth_method || "consumer",
      client_id: process.env.AGY_OAUTH_CLIENT_ID,
      client_secret: process.env.AGY_OAUTH_CLIENT_SECRET,
      // Seeded expired on purpose: the broker refreshes once on the first
      // getToken and thereby proves it can, instead of handing out a token that
      // happens to still be warm and failing an hour later.
      expires_at: 0
    };
  },
  // GLM is a static key — read from ZAI_API_KEY or the local opencode auth file.
  // Seeded "fresh forever" so the broker just hands it out (no refresh).
  glm() {
    let key = process.env.ZAI_API_KEY;
    if (!key) {
      try {
        const a = JSON.parse(fs.readFileSync(path.join(HOME, ".local/share/opencode/auth.json"), "utf8"));
        key = a["zai-coding-plan"] && a["zai-coding-plan"].key;
      } catch (_e) {
        // none locally
      }
    }
    return {
      provider: "glm",
      api_key: key,
      refresh_token: key, // satisfies seedToken's required-field check
      access_token: key,
      expires_at: Date.now() + 10 * 365 * 24 * 3600 * 1000
    };
  }
};

async function seed(provider, account = "default") {
  const load = LOADERS[provider];
  if (!load) throw new Error(`unknown provider: ${provider} (codex|claude|agy|gemini|glm)`);
  const cfg = config.require();
  const body = { ...load(), account };
  if (!body.refresh_token) {
    throw new Error(`${provider}: no refresh_token in local auth file — log in first`);
  }
  if (provider === "agy" && !(body.client_id && body.client_secret)) {
    throw new Error(
      "agy: set AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET for the seed command"
    );
  }

  const seedResp = await fetch(`${cfg.url}/seedToken`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-broker-key": cfg.key },
    body: JSON.stringify(body)
  });
  if (!seedResp.ok) {
    throw new Error(`seedToken failed ${seedResp.status}: ${(await seedResp.text()).slice(0, 160)}`);
  }

  // Verify the broker can actually refresh it.
  const getResp = await fetch(`${cfg.url}/getToken?provider=${provider}&account=${encodeURIComponent(account)}&format=authjson`, {
    headers: { "x-broker-key": cfg.key }
  });
  if (getResp.status === 200) {
    return { ok: true, provider };
  }
  throw new Error(`getToken after seed: ${getResp.status} ${(await getResp.text()).slice(0, 160)}`);
}

module.exports = { seed, LOADERS };
