const { log, error } = require("firebase-functions/logger");

const store = require("./store");
const { providers, secretsFor, codexHandle, authorized, isDeadGrant, alertOnce } = require("./shared");

// GET /getToken?provider=codex[&account=default][&format=authjson|raw]
// timeoutSeconds > the withRefreshLease poll-wait deadline (LEASE_MS+2s) so the
// platform can't kill a waiter before it returns.
async function getToken(req, res) {
  if (!(await authorized(req))) return res.status(401).json({ error: "unauthorized" });

  const provider = String(req.query.provider || "");
  const account = String(req.query.account || "default");
  const format = String(req.query.format || "raw");
  const p = providers[provider];
  if (!p) return res.status(400).json({ error: `unknown provider: ${provider}` });

  try {
    let token = await store.read(provider, account);
    // A stub with no refresh token cannot be refreshed into anything; say so
    // plainly instead of failing every request with a refresh error.
    if (!token || !(token.refresh_token || token.api_key)) {
      return res.status(404).json({ error: "not_seeded", provider, account });
    }

    if (!store.accessIsFresh(token)) {
      const cfg = await store.readConfig();
      token = await store.withRefreshLease(provider, account, (prev) =>
        p.refresh(prev, secretsFor(cfg, provider))
      );
    }

    if (format === "authjson") {
      if (provider === "codex") {
        const cfg = await store.readConfig();
        // Rollout flag (strict === true so a stray "false"/1 can't enable it): ONCE
        // ON, codex's authjson carries the opaque handle (never the real token), which
        // also covers the CI `broker get authjson` path. While OFF (default) it keeps
        // emitting the real token (pre-Phase-1 behaviour), so deploying this code does
        // not change field behaviour and no client breaks. Flip on only after every
        // cx/CI is updated — see §6.8.
        const authFile = cfg.codex_handle_rollout === true
          ? p.toAuthFile(token, codexHandle(cfg, account))
          : p.toLegacyAuthFile(token);
        return res.status(200).json(authFile);
      }
      return res.status(200).json(p.toAuthFile(token));
    }
    return res.status(200).json({ access_token: token.access_token, expires_at: token.expires_at });
  } catch (err) {
    error(`getToken ${provider}/${account} failed`, err);
    if (isDeadGrant(err.message)) {
      await alertOnce(
        provider,
        account,
        `:rotating_light: token-broker: ${provider}/${account} needs re-auth — ${err.message}`
      );
      return res.status(401).json({ error: "needs_reauth", provider, account });
    }
    // 503 (retryable), matching /oauthRefresh's transient contract — a transient
    // failure must not read as a permanent error to a retrying client.
    return res.status(503).json({ error: "refresh_failed", message: err.message });
  }
}

// GET /listAccounts?provider=codex — names of the seeded accounts, nothing else.
// The wrappers use it to know who they may pick from; no token data is exposed.
async function listAccounts(req, res) {
  if (!(await authorized(req))) return res.status(401).json({ error: "unauthorized" });

  const provider = String(req.query.provider || "");
  if (!providers[provider]) return res.status(400).json({ error: `unknown provider: ${provider}` });

  try {
    const accounts = await store.listAccounts(provider);
    return res.status(200).json({ provider, accounts });
  } catch (err) {
    error(`listAccounts ${provider} failed`, err);
    return res.status(500).json({ error: "list_failed", message: err.message });
  }
}

// POST /seedToken { provider, account?, refresh_token, client_id?, ...fields }
// Called once after a local login to hand the broker ownership of the refresh token.
async function seedToken(req, res) {
  if (!(await authorized(req))) return res.status(401).json({ error: "unauthorized" });
  if (req.method !== "POST") return res.status(405).json({ error: "use POST" });

  const { provider, account = "default", ...fields } = req.body || {};
  if (!providers[provider]) return res.status(400).json({ error: `unknown provider: ${provider}` });
  if (!fields.refresh_token) return res.status(400).json({ error: "refresh_token required" });

  await store.write(provider, account, {
    ...fields,
    expires_at: fields.expires_at || 0, // force a refresh on first getToken
    lease_until: 0,
    lease_owner: null,
    // Re-seeding is the documented recovery for a wedged rotation — it MUST clear
    // the journal, or a once-stuck account stays permanently unrefreshable. Also
    // reset the alert throttle so the FIRST failure of the new token can page again.
    rotation_pending: false,
    last_reauth_alert: 0
  });
  log(`seeded ${provider}/${account}`);
  return res.status(200).json({ ok: true, provider, account });
}

// POST /deleteAccount?provider=codex&account=<name> — forget an account.
// The refresh token goes with it, so this is how access is actually revoked
// from the broker's side; re-adding means a fresh login plus a new seed.
async function deleteAccount(req, res) {
  if (!(await authorized(req))) return res.status(401).json({ error: "unauthorized" });
  if (req.method !== "POST" && req.method !== "DELETE") {
    return res.status(405).json({ error: "use POST or DELETE" });
  }

  const provider = String(req.query.provider || (req.body && req.body.provider) || "");
  const account = String(req.query.account || (req.body && req.body.account) || "");
  if (!providers[provider]) return res.status(400).json({ error: `unknown provider: ${provider}` });
  if (!account) return res.status(400).json({ error: "account required" });

  try {
    const existed = await store.remove(provider, account);
    log(`deleted ${provider}/${account} (existed: ${existed})`);
    return res.status(200).json({ ok: true, provider, account, existed });
  } catch (err) {
    error(`deleteAccount ${provider}/${account} failed`, err);
    return res.status(500).json({ error: "delete_failed", message: err.message });
  }
}

module.exports = { getToken, listAccounts, seedToken, deleteAccount };
