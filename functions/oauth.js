const { log, error } = require("firebase-functions/logger");

const store = require("./store");
const { providers, secretsFor, codexHandle, handleMatches, isDeadGrant, alertOnce } = require("./shared");

// POST /oauthRefresh?provider=codex&account=<a>
// codex's OWN refresh endpoint, reached via CODEX_REFRESH_TOKEN_URL_OVERRIDE. codex
// POSTs {client_id, grant_type, refresh_token:<handle>} with NO broker-key header,
// so the handle IS the credential: recompute and compare it. The real refresh runs
// through the SAME withRefreshLease path getToken uses — one fenced writer, never a
// parallel one. This is what routes codex's own mid-session refreshes through the
// broker so the real OpenAI token stays put.
async function oauthRefresh(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "use POST" });

  const provider = String(req.query.provider || "codex");
  const account = String(req.query.account || "default");
  const p = providers[provider];
  if (provider !== "codex" || !p) {
    return res.status(400).json({ error: `unsupported provider: ${provider}` });
  }

  // codex 0.148.0 treats a plain 400 invalid_grant as TRANSIENT (it keeps retrying);
  // only HTTP 401 (or a refresh_token_* body code) is a permanent re-auth signal. So
  // every "codex must re-authenticate" outcome here returns 401, not 400.
  const reauth = (r) => r.status(401).json({ error: "invalid_grant" });

  const presented = String((req.body && req.body.refresh_token) || "");
  try {
    const cfg = await store.readConfig();

    // Wrong/missing handle => codex must re-auth (constant-time compare).
    const expected = codexHandle(cfg, account);
    if (!handleMatches(presented, expected)) return reauth(res);

    const token = await store.read(provider, account);
    if (!token || !token.refresh_token) return reauth(res);

    // codex only calls this when it wants a genuinely NEW token (proactively at
    // exp-5min, or after a 401 on the current one). Force a rotation through the
    // fenced lease, coalescing concurrent callers onto one real OpenAI refresh — a
    // time-freshness gate here would hand back the same token and make codex loop.
    const fresh = await store.withRefreshLease(
      provider,
      account,
      (prev) => p.refresh(prev, secretsFor(cfg, provider)),
      { coalesceFrom: token.access_token }
    );

    // codex derives expiry from the access-JWT `exp` and ignores expires_in, but
    // send it defensively. refresh_token stays the same constant handle.
    const expiresIn = Math.max(1, Math.floor(((fresh.expires_at || 0) - Date.now()) / 1000));
    return res.status(200).json({
      access_token: fresh.access_token,
      id_token: fresh.id_token,
      refresh_token: expected,
      token_type: "Bearer",
      expires_in: expiresIn
    });
  } catch (err) {
    error(`oauthRefresh ${provider}/${account} failed`, err);
    // A genuinely dead grant => 401 (permanent re-auth to codex). A transient
    // broker/OpenAI failure must NOT wipe codex's auth — 503 so codex retries later.
    const msg = err.message || "";
    if (isDeadGrant(msg)) {
      await alertOnce(
        provider,
        account,
        `:rotating_light: token-broker: ${provider}/${account} needs re-auth (oauthRefresh) — ${msg}`
      );
      return reauth(res);
    }
    return res.status(503).json({ error: "temporarily_unavailable", message: msg });
  }
}

// POST /oauthRevoke?provider=codex&account=<a>
// codex logout posts {token:<handle>, token_type_hint, client_id} here (via
// CODEX_REVOKE_TOKEN_URL_OVERRIDE). Accounts are SHARED, so one user's logout must
// NOT revoke the grant for CI/others: ack 200 and do NOT touch the stored token.
// Real revocation is an explicit `broker deleteAccount`.
async function oauthRevoke(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "use POST" });
  const provider = String(req.query.provider || "codex");
  const account = String(req.query.account || "default");
  if (provider !== "codex") return res.status(400).json({ error: `unsupported provider: ${provider}` });

  // Validate the handle (constant-time) but ALWAYS ack 200: accounts are SHARED, so
  // one user's `codex logout` must NOT revoke the grant for CI/others. Real
  // revocation is an explicit `broker deleteAccount`. Verifying at least keeps this
  // from being a wholly unauthenticated surface and marks noise in the log.
  let ok = false;
  try {
    const cfg = await store.readConfig();
    ok = handleMatches((req.body && req.body.token) || "", codexHandle(cfg, account));
  } catch (_e) {
    ok = false;
  }
  log(`oauthRevoke ${provider}/${account} — no-op (handle ${ok ? "valid" : "invalid"}; shared grant preserved; use deleteAccount to revoke)`);
  return res.status(200).json({ ok: true });
}

module.exports = { oauthRefresh, oauthRevoke };
