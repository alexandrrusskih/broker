const crypto = require("crypto");
const { fetchWithRetry } = require("../fetch");

// Codex / ChatGPT OAuth. The refresh token is SINGLE-USE and ROTATES — this is
// the whole reason the broker exists: only this code path ever refreshes it, so
// no sibling client can trigger `refresh_token_reused`.
//
// VERIFY ON FIRST SEED: endpoint + client_id are taken from the codex CLI auth
// flow and may need confirming against a real `~/.codex/auth.json`. The seed
// payload carries client_id so we don't hardcode a wrong one.
const TOKEN_URL = "https://auth.openai.com/oauth/token";

// A refresh token is SINGLE-USE and rotates. A hard timeout, strictly shorter
// than store.js LEASE_MS, guarantees a hung OpenAI call can never outlive the
// refresh lease and let a second holder submit the same token to OpenAI.
const REFRESH_TIMEOUT_MS = 20_000;

async function refresh(prev, secrets) {
  if (!prev?.refresh_token) throw new Error("codex: no refresh_token to renew");
  const clientId = prev.client_id || secrets.client_id;
  if (!clientId) throw new Error("codex: missing client_id (seed it from auth.json)");

  const body = JSON.stringify({
    grant_type: "refresh_token",
    refresh_token: prev.refresh_token,
    client_id: clientId,
    scope: prev.scope || "openid profile email offline_access"
  });

  // retries=0: NEVER replay a single-use refresh. fetchWithRetry retries transport
  // errors and 5xx by default — but if OpenAI consumed and rotated the token before
  // the response was lost, replaying the same token re-submits a spent secret and
  // trips reuse detection, invalidating the whole grant. An ambiguous failure must
  // bubble up (the lease holder fails, no second submission), never retry.
  const resp = await fetchWithRetry(
    TOKEN_URL,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      signal: AbortSignal.timeout(REFRESH_TIMEOUT_MS)
    },
    0,
    0,
    { throwOnHttpError: false }
  );
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(`codex refresh failed ${resp.status}: ${txt.slice(0, 200)}`);
  }
  const t = await resp.json();
  // A malformed 2xx without an access token must NOT commit (it would clear the
  // rotation journal and leave the account with no usable credential). Treat it as
  // a failed refresh so the lease releases without persisting garbage.
  if (!t || !t.access_token) {
    throw new Error("codex refresh failed: 2xx response missing access_token");
  }
  return {
    access_token: t.access_token,
    id_token: t.id_token || prev.id_token,
    refresh_token: t.refresh_token || prev.refresh_token, // rotates — keep the new one
    expires_at: Date.now() + (t.expires_in || 3600) * 1000,
    client_id: clientId,
    account_id: prev.account_id,
    scope: t.scope || prev.scope
  };
}

// The opaque handle codex holds INSTEAD of the real refresh_token: a constant,
// account-scoped HMAC. It is meaningless to OpenAI, so codex writing it back and
// re-submitting it on its own refresh is harmless — the broker recomputes and
// verifies it, then refreshes the REAL token internally (see /oauthRefresh). This
// is what keeps the real single-use token from ever leaving the broker.
function refreshHandle(handleSecret, account) {
  return crypto.createHmac("sha256", handleSecret).update(`codex:${account}`).digest("hex");
}

// Render ~/.codex/auth.json. CODEX_API_KEY/OPENAI_API_KEY are intentionally
// absent so codex uses the OAuth tokens (ChatGPT subscription, not API billing).
function buildAuthFile(tokenData, refreshTokenValue) {
  return {
    auth_mode: "chatgpt",
    OPENAI_API_KEY: null,
    tokens: {
      id_token: tokenData.id_token,
      access_token: tokenData.access_token,
      refresh_token: refreshTokenValue,
      account_id: tokenData.account_id
    },
    last_refresh: new Date(tokenData.updated_at || Date.now()).toISOString()
  };
}

// Handle mode (rollout ON): `handle` REPLACES the real refresh_token — the single
// substitution point, so the real token never reaches disk. A missing handle, or the
// real token passed AS the handle, is a hard error, never a silent real-token leak.
function toAuthFile(tokenData, handle) {
  if (!handle || handle === tokenData.refresh_token) {
    throw new Error("codex toAuthFile: valid handle required (refusing to emit the real refresh_token)");
  }
  return buildAuthFile(tokenData, handle);
}

// Legacy mode (rollout OFF): emit the REAL refresh_token, exactly as before Phase 1.
// Used ONLY while broker_config.codex_handle_rollout is unset, so DEPLOYING the
// broker does not break field clients that predate the override wiring: getToken
// keeps handing out the real token (today's behaviour) until every cx/CI is updated
// and the flag is flipped. cx tells the two apart by the token's shape (a handle is
// 64-hex) and only routes through /oauthRefresh when it sees a handle. Delete this
// once the rollout is complete.
function toLegacyAuthFile(tokenData) {
  return buildAuthFile(tokenData, tokenData.refresh_token);
}

module.exports = { refresh, toAuthFile, toLegacyAuthFile, refreshHandle };
