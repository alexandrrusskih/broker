const config = require("./config");
const { readJson } = require("../functions/stores/files");
const { openPrompter } = require("./prompt");

async function setup(options = {}, deps = {}) {
  const cfg = deps.config || config;
  const current = cfg.read(false);
  // Installers do not silently re-point an existing installation. Explicit
  // changes remain the job of `broker config` or selecting BROKER_CONFIG.
  if (current.url && current.key) {
    return { configured: true, changed: false, file: cfg.FILE };
  }
  const ui = (deps.openPrompter || openPrompter)(options.noAsk);
  try {
    let connection;
    if (options.clientConfig) {
      const supplied = await readJson(options.clientConfig);
      if (!supplied?.url || !supplied.key) throw new Error("client config must contain url and key");
      if (supplied.role === "admin") throw new Error("use client.json, not the server's admin config");
      connection = { url: supplied.url, key: supplied.key };
      if (supplied.account) connection.account = supplied.account;
      if (supplied.accounts) connection.accounts = supplied.accounts;
      if (supplied.role) connection.role = supplied.role;
    } else {
      const effective = cfg.read();
      // Runtime-only configuration is intentional (notably in Docker). Do not
      // turn environment secrets into persistent image/config contents.
      if (!options.url && effective.url && effective.key) return { configured: true, changed: false, runtime: true };
      if (!options.url) {
        if (!ui || !/^y(es)?$/i.test((await ui.ask("Configure a broker connection now? [y/N] ")) || "")) {
          return { configured: false, changed: false };
        }
      }
      const url = options.url || await ui?.ask(`Broker URL${effective.url ? ` [${effective.url}]` : ""}: `) || effective.url;
      if (!url) throw new Error("broker URL required; pass --url or --client-config in non-interactive mode");
      // A newly supplied URL must not inherit an unrelated saved key.
      const key = current.url && current.url !== url ? null : effective.key;
      const entered = key || await ui?.ask("Client key (hidden): ", { secret: true });
      if (!entered) throw new Error("client key required; provide a client config file or BROKER_KEY");
      connection = { url, key: entered };
    }
    const address = new URL(connection.url);
    if (!["http:", "https:"].includes(address.protocol) || address.username || address.password) {
      throw new Error("broker URL must be HTTP(S), without embedded credentials");
    }
    connection.url = connection.url.replace(/\/+$/, "");
    cfg.write(connection);
    return { configured: true, changed: true, file: cfg.FILE };
  } finally { ui?.close(); }
}

module.exports = { setup };
