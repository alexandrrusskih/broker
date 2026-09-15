const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const fs = require("node:fs/promises");
const crypto = require("node:crypto");
const { createStore } = require("../functions/store");
const { createFileAdapter, writeJson } = require("../functions/stores/files");
const { createRouter } = require("../functions/router");

const DEFAULT_DATA_DIR = path.join(os.homedir(), ".local", "share", "hltm-broker");

async function init({ dataDir = DEFAULT_DATA_DIR, url = "http://127.0.0.1:8787", adminUrl = "http://127.0.0.1:8787" } = {}) {
  const address = new URL(url);
  if (address.username || address.password || address.search || address.hash ||
      !(address.protocol === "https:" || (address.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(address.hostname)))) {
    throw new Error("use an HTTPS broker URL (HTTP is allowed only on loopback)");
  }
  const root = path.resolve(dataDir);
  const store = createStore(createFileAdapter(root));
  const settings = {
    broker_key: crypto.randomBytes(32).toString("hex"),
    client_key: crypto.randomBytes(32).toString("hex"),
    codex_handle_secret: crypto.randomBytes(32).toString("hex"),
    codex_handle_rollout: true,
    created_at: Date.now()
  };
  if (!(await store.claimConfig(settings))) throw new Error("broker already initialized; existing keys were not changed");
  const clientFile = path.join(root, "client.json");
  const adminFile = path.join(root, "admin.json");
  const base = { url: address.toString().replace(/\/+$/, "") };
  await writeJson(clientFile, { ...base, key: settings.client_key, role: "client" });
  // Admin operations normally run on the server itself. Do not depend on its
  // public DNS/Tailscale endpoint to recover or seed an account locally.
  await writeJson(adminFile, { url: adminUrl, key: settings.broker_key, role: "admin" });
  await fs.mkdir(path.join(root, "logs"), { mode: 0o700, recursive: true });
  return { dataDir: root, clientFile, adminFile };
}

// Small adapter for the same request/response contract Firebase supplies. No
// reverse proxy, Firebase SDK or Express is needed in the self-hosted process.
function createHttpServer(store, options = {}) {
  const route = createRouter(store, { ...options, handlesOnly: true });
  const server = http.createServer(async (req, res) => {
    res.status = (code) => { res.statusCode = code; return res; };
    res.json = (body) => {
      res.setHeader("Content-Type", "application/json; charset=utf-8");
      res.setHeader("Cache-Control", "no-store");
      res.setHeader("X-Content-Type-Options", "nosniff");
      res.end(JSON.stringify(body));
    };
    req.get = (name) => req.headers[name.toLowerCase()];
    try {
      const url = new URL(req.url, "http://localhost");
      req.path = url.pathname;
      req.query = Object.fromEntries(url.searchParams);
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1_048_576) return res.status(413).json({ error: "body_too_large" });
        chunks.push(chunk);
      }
      const body = Buffer.concat(chunks).toString("utf8");
      const type = (req.headers["content-type"] || "").split(";")[0].trim();
      if (!body) req.body = {};
      else if (type === "application/json") req.body = JSON.parse(body);
      else if (type === "application/x-www-form-urlencoded") req.body = Object.fromEntries(new URLSearchParams(body));
      else return res.status(415).json({ error: "use JSON or form encoding" });
    } catch (_err) {
      return res.status(400).json({ error: "invalid_request" });
    }
    await route(req, res);
  });
  server.requestTimeout = 120_000;
  return server;
}

async function serve({ dataDir = DEFAULT_DATA_DIR, host = "127.0.0.1", port = 8787, logger } = {}) {
  const store = createStore(createFileAdapter(dataDir));
  const cfg = await store.readConfig();
  if (!cfg.broker_key || !cfg.client_key || !cfg.codex_handle_secret) throw new Error("run broker init before broker serve");
  const server = createHttpServer(store, { logger });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => { server.off("error", reject); resolve(); });
  });
  return server;
}

module.exports = { init, serve, createHttpServer, DEFAULT_DATA_DIR };
