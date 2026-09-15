const { createTokenHandlers } = require("./tokens");
const { createOAuthHandlers } = require("./oauth");
const { createAdminHandlers } = require("./admin");

function createRouter(store, options = {}) {
  const routes = {
    ...createTokenHandlers(store, options),
    ...createOAuthHandlers(store, options),
    ...createAdminHandlers(store, options)
  };
  if (options.handlesOnly) delete routes.bootstrap; // self-hosted provisioning is local CLI only
  const logger = options.logger || console;
  return async (req, res) => {
    const action = String(req.path || "").replace(/^\/+/, "").split("/")[0] || String(req.query.action || "");
    const handler = Object.hasOwn(routes, action) && routes[action];
    if (!handler) return res.status(404).json({ error: `unknown action: ${action || "(none)"}`, actions: Object.keys(routes) });
    try { return await handler(req, res); }
    catch (err) {
      logger.error(`broker/${action} crashed`, err);
      if (!res.headersSent) res.status(500).json({ error: "internal", action });
    }
  };
}

module.exports = { createRouter };
