const { fetchWithRetry } = require("../fetch");

// Google OAuth — standard, well-documented. Refresh token does NOT rotate, so
// concurrent use is naturally safe, but we still route it through the broker for
// a single uniform interface.
const TOKEN_URL = "https://oauth2.googleapis.com/token";

// refresh(prev, secrets) -> { access_token, refresh_token, expires_at, ...passthrough }
async function refresh(prev, secrets) {
  if (!prev?.refresh_token) throw new Error("gemini: no refresh_token to renew");
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: prev.refresh_token,
    client_id: secrets.client_id,
    client_secret: secrets.client_secret
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
    throw new Error(`gemini refresh failed ${resp.status}: ${txt.slice(0, 200)}`);
  }
  const t = await resp.json();
  return {
    access_token: t.access_token,
    refresh_token: prev.refresh_token, // Google does not return a new refresh token
    expires_at: Date.now() + (t.expires_in || 3600) * 1000,
    token_type: t.token_type || "Bearer",
    scope: t.scope || prev.scope
  };
}

// Render the on-disk form the gemini CLI expects (~/.gemini/oauth_creds.json).
function toAuthFile(tokenData) {
  return {
    access_token: tokenData.access_token,
    refresh_token: tokenData.refresh_token,
    token_type: tokenData.token_type || "Bearer",
    expiry_date: tokenData.expires_at,
    scope: tokenData.scope
  };
}

module.exports = { refresh, toAuthFile };
