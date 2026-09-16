const os = require("node:os");
const path = require("node:path");
const config = require("./config");
const { readJson, writeJson } = require("../functions/stores/files");

function address(value) {
  let url;
  try { url = new URL(value); }
  catch { throw new Error("container broker URL required; use --url <https-url> or --client-config <file>"); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("container broker URL must be HTTP(S), without credentials, query or fragment");
  }
  return url;
}

function isLoopback(url) {
  const host = url.hostname.replace(/\.$/, "");
  return host === "localhost" || host.endsWith(".localhost") || host === "[::1]" || /^127\./.test(host);
}

// Configure the Docker HOST's Medulla overlay, not the host's own Codex.
// Docker Desktop and Colima consume the same mounts. Neither is restarted.
async function setupContainer(options = {}, deps = {}) {
  const home = deps.home || os.homedir();
  const cfg = deps.config || config;
  const root = path.join(home, ".medulla", "container");
  const file = path.join(root, "home", ".config", "hltm-broker", "config.json");
  let sourceFile = options.clientConfig ? path.resolve(options.clientConfig) : file;
  let source = await readJson(sourceFile);
  if (options.clientConfig && !source) throw new Error("client config file not found");
  if (!source) {
    source = cfg.read();
    sourceFile = cfg.FILE;
  }
  if (source.role === "admin") throw new Error("use client.json, not the server's admin config, for containers");

  // A server's own client often uses loopback. Its managed service already knows
  // the externally reachable URL and CLIENT key; never read settings/admin.json.
  if (!options.clientConfig && !options.url && (!source.url || isLoopback(address(source.url)))) {
    const service = await readJson(path.join(home, ".local", "share", "hltm-broker-service", "service.json"));
    if (service?.dataDir) {
      const candidateFile = path.join(service.dataDir, "client.json");
      const candidate = await readJson(candidateFile);
      if (candidate?.role === "client" && (!source.key || source.key === candidate.key)) {
        source = candidate;
        sourceFile = candidateFile;
      }
    }
  }

  if (!source.key) throw new Error("client key required; run broker setup or pass --client-config <file>");
  const url = address(options.url || source.url);
  if (isLoopback(url)) {
    throw new Error("loopback points at the container itself; pass --url <reachable-broker-url> or --client-config <file>");
  }
  const connection = { url: url.toString().replace(/\/+$/, ""), key: source.key };
  // Keep the selected client's account preferences, not machine paths or tokens.
  for (const key of ["role", "account", "accounts", "min_headroom"]) {
    if (source[key] !== undefined) connection[key] = source[key];
  }

  const install = deps.installOverlay || require("./wrap").installContainer;
  const wrapper = install("codex", root);
  await writeJson(file, connection);
  return { file, wrapper, url: connection.url, source: sourceFile };
}

module.exports = { setupContainer };
