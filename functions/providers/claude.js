const { fetchWithRetry } = require("../fetch");

// Claude (Anthropic Max) OAuth. Refresh token ROTATES (like codex), so the
// broker being the sole refresh authority removes the local<->CI race and the
// current manual `CLAUDE_OAUTH_TOKEN` re-pasting.
//
// VERIFY ON FIRST SEED: endpoint + client_id from the Claude Code OAuth flow;
// confirm against a real ~/.claude/.credentials.json. client_id comes from seed.
const TOKEN_URL = "https://console.anthropic.com/v1/oauth/token";

async function refresh(prev, secrets) {
  if (!prev?.refresh_token) throw new Error("claude: no refresh_token to renew");
  const clientId = prev.client_id || secrets.client_id;
  if (!clientId) throw new Error("claude: missing client_id (seed it)");

  const body = JSON.stringify({
    grant_type: "refresh_token",
    refresh_token: prev.refresh_token,
    client_id: clientId
  });

  const resp = await fetchWithRetry(
    TOKEN_URL,
    { method: "POST", headers: { "Content-Type": "application/json" }, body },
    2,
    400,
    { throwOnHttpError: false }
  );
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(`claude refresh failed ${resp.status}: ${txt.slice(0, 200)}`);
  }
  const t = await resp.json();
  return {
    access_token: t.access_token,
    refresh_token: t.refresh_token || prev.refresh_token, // rotates
    expires_at: Date.now() + (t.expires_in || 3600) * 1000,
    client_id: clientId,
    scope: t.scope || prev.scope,
    subscription_type: prev.subscription_type || "max"
  };
}

// Render ~/.claude/.credentials.json (native Claude Code format).
function toAuthFile(tokenData) {
  return {
    claudeAiOauth: {
      accessToken: tokenData.access_token,
      refreshToken: tokenData.refresh_token,
      expiresAt: tokenData.expires_at,
      scopes: (tokenData.scope || "user:inference").split(" "),
      subscriptionType: tokenData.subscription_type || "max",
      rateLimitTier: tokenData.rate_limit_tier || "default_claude_max_5x"
    }
  };
}

module.exports = { refresh, toAuthFile };
