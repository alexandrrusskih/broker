const { onRequest } = require("firebase-functions/v2/https");
const { error } = require("firebase-functions/logger");
const admin = require("firebase-admin");

admin.initializeApp();
admin.firestore().settings({ ignoreUndefinedProperties: true });

const tokens = require("./tokens");
const oauth = require("./oauth");
const adminApi = require("./admin");

// One function, one cold start, one URL. The action is the path segment, so
// `<base>/broker/getToken?...` reaches getToken with `req.path === "/getToken"`
// — clients only need `url` pointed at `<base>/broker`, nothing else changes.
// `?action=` is accepted too, for callers that cannot shape a path.
const ROUTES = {
  getToken: tokens.getToken,
  listAccounts: tokens.listAccounts,
  seedToken: tokens.seedToken,
  deleteAccount: tokens.deleteAccount,
  // codex's own OAuth callbacks, reached via CODEX_*_TOKEN_URL_OVERRIDE.
  oauthRefresh: oauth.oauthRefresh,
  oauthRevoke: oauth.oauthRevoke,
  bootstrap: adminApi.bootstrap,
  configSet: adminApi.configSet,
  configGet: adminApi.configGet
};

// timeoutSeconds covers the slowest route (getToken/oauthRefresh wait on the
// refresh lease, LEASE_MS+2s), so the platform can't kill a legitimate waiter.
exports.broker = onRequest(
  { cors: false, timeoutSeconds: 120, secrets: ["BROKER_BOOTSTRAP_TOKEN"] },
  async (req, res) => {
    const action =
      String(req.path || "").replace(/^\/+/, "").split("/")[0] || String(req.query.action || "");
    const handler = ROUTES[action];
    if (!handler) {
      return res
        .status(404)
        .json({ error: `unknown action: ${action || "(none)"}`, actions: Object.keys(ROUTES) });
    }
    try {
      return await handler(req, res);
    } catch (err) {
      // A handler that throws past its own try/catch must not leak a stack to the
      // caller — and must not hang the request either.
      error(`broker/${action} crashed`, err);
      if (!res.headersSent) res.status(500).json({ error: "internal", action });
      return undefined;
    }
  }
);
