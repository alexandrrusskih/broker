const { fetchWithRetry } = require("../fetch");

const TOKEN_URL = "https://oauth2.googleapis.com/token";

// Google does NOT rotate refresh tokens for this grant type: the same
// refresh_token keeps working, and two machines refreshing at once each get their
// own access_token without invalidating the other. That is the whole reason agy
// needs none of the handle/lease machinery codex required — there is no reuse
// detection to trip. The broker owns the refresh anyway, so one account can be
// handed out from one place and its usage stays legible.
async function refresh(prev, secrets) {
  if (!prev?.refresh_token) throw new Error("agy: no refresh_token to renew");
  const clientId = prev.client_id || secrets.client_id;
  const clientSecret = prev.client_secret || secrets.client_secret;
  if (!clientId || !clientSecret) {
    throw new Error(
      "agy: missing OAuth client credentials; seed with AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET"
    );
  }
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: prev.refresh_token,
    client_id: clientId,
    client_secret: clientSecret
  });

  const resp = await fetchWithRetry(
    TOKEN_URL,
    { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body },
    2,
    400,
    { throwOnHttpError: false }
  );
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(`agy refresh failed ${resp.status}: ${txt.slice(0, 200)}`);
  }
  const t = await resp.json();
  return {
    access_token: t.access_token,
    refresh_token: prev.refresh_token, // Google returns no new one, and needs none
    expires_at: Date.now() + (t.expires_in || 3600) * 1000,
    token_type: t.token_type || "Bearer",
    scope: t.scope || prev.scope,
    auth_method: prev.auth_method || "consumer"
  };
}

// The on-disk shape agy expects at ~/.gemini/antigravity-cli/antigravity-oauth-token.
// `expiry` is read by Go's time parser, so an ISO-8601 instant is what it wants.
function toAuthFile(tokenData) {
  return {
    auth_method: tokenData.auth_method || "consumer",
    token: {
      access_token: tokenData.access_token,
      refresh_token: tokenData.refresh_token,
      token_type: tokenData.token_type || "Bearer",
      expiry: new Date(tokenData.expires_at || Date.now()).toISOString()
    }
  };
}

module.exports = { refresh, toAuthFile };
