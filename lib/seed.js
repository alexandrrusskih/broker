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
    // whose HOME this process was handed (broker-agy auth sets it); BROKER_AGY_HOME
    // overrides it for a seed run outside that flow.
    const home = process.env.BROKER_AGY_HOME
      ? path.resolve(process.env.BROKER_AGY_HOME.replace(/^~(?=$|\/)/, HOME))
      : HOME;
    const file = path.join(home, ".gemini", "antigravity-cli", "antigravity-oauth-token");
    const a = JSON.parse(fs.readFileSync(file, "utf8"));
    const t = a.token || {};
    return {
      provider: "agy",
      refresh_token: t.refresh_token,
      access_token: t.access_token,
      token_type: t.token_type || "Bearer",
      // Who the session belongs to, not permission to have one. agy's consumer
      // auth refuses a token without it — "You are not logged into
      // Antigravity" — and the refresh never returns one, so the broker has to
      // carry the id_token it was seeded with, the way codex does above.
      // Leaving it out hid behind a local login: a machine that had run agy
      // once had an id_token of its own in ~/.gemini and never missed this,
      // while a machine seeded from the broker alone got a token file 922
      // characters shorter and an account that looked authorized and would not
      // start.
      id_token: a.id_token || t.id_token,
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
  // agy's OAuth client credential belongs to the APPLICATION, not to whoever
  // is seeding, so requiring it from this machine's environment was wrong: it
  // turned "the broker has not been told agy's client id" into "you cannot add
  // your account", on every machine separately. The broker can hold it once in
  // its own config (agy_client_id / agy_client_secret) for the whole fleet —
  // see functions/providers/agy.js, which falls back to exactly that. So the
  // local values are an override when present and absent otherwise, and the
  // verify call below is the honest test of whether EITHER side has it.
  const localClient = provider === "agy" && Boolean(body.client_id && body.client_secret);
  if (provider === "agy" && !localClient) {
    delete body.client_id;
    delete body.client_secret;
  }

  const seedResp = await fetch(`${cfg.url}/seedToken`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-broker-key": cfg.key },
    body: JSON.stringify(body)
  });
  if (!seedResp.ok) {
    // 401 here is almost never a wrong key — it is the RIGHT key for the wrong
    // job. Seeding is admin-only (functions/tokens.js, authorized(req, true)),
    // while fetching tokens accepts the client key, so a teammate handed a
    // client key can run everything except this one command. Said plainly,
    // because "unauthorized" sent people to re-check a key that was fine.
    if (seedResp.status === 401) {
      throw new Error(
        "seeding is refused: this broker key may FETCH tokens but not seed them. " +
        "Seeding needs the broker's admin key. Ask whoever runs the broker to seed " +
        `'${provider}' for account '${account}', or to give you an admin key — ` +
        "then rerun this with BROKER_CONFIG pointing at that config."
      );
    }
    throw new Error(`seedToken failed ${seedResp.status}: ${(await seedResp.text()).slice(0, 160)}`);
  }

  // Verify the broker can actually refresh it.
  const getResp = await fetch(`${cfg.url}/getToken?provider=${provider}&account=${encodeURIComponent(account)}&format=authjson`, {
    headers: { "x-broker-key": cfg.key }
  });
  if (getResp.status === 200) {
    return { ok: true, provider };
  }
  const said = (await getResp.text()).slice(0, 160);
  // The one failure that is a setup question rather than a fault: neither this
  // machine nor the broker holds agy's client credential. Said once, with both
  // ways out, because the server's own wording names only the first.
  if (provider === "agy" && !localClient && /client credential/i.test(said)) {
    throw new Error(
      "agy: no OAuth client credential on this machine and none in the broker's config. " +
      "Either set AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET for this one command, " +
      "or have whoever runs the broker set agy_client_id and agy_client_secret in its " +
      "config once, for everyone."
    );
  }
  throw new Error(`getToken after seed: ${getResp.status} ${said}`);
}

module.exports = { seed, LOADERS };
