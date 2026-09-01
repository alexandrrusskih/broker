// GLM (z.ai `zai-coding-plan`) — STATIC api key, NOT OAuth. There is nothing to
// rotate or refresh; the broker simply stores the key and hands it out, so all
// machines/CI get auth from one place (and the spar glm5 panelist stops failing
// on a missing zai key). Seeded with expires_at far in the future, so getToken
// always serves it as "fresh" and refresh() is effectively never called.
async function refresh(prev) {
  const key = prev.api_key || prev.access_token || prev.refresh_token;
  return {
    access_token: key,
    refresh_token: key,
    api_key: key,
    expires_at: Date.now() + 10 * 365 * 24 * 3600 * 1000
  };
}

// Render ~/.local/share/opencode/auth.json (opencode zai provider entry).
function toAuthFile(tokenData) {
  const key = tokenData.api_key || tokenData.access_token;
  return { "zai-coding-plan": { type: "api", key } };
}

module.exports = { refresh, toAuthFile };
