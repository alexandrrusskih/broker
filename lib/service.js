const fs = require("node:fs/promises");
const syncFs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { init, DEFAULT_DATA_DIR } = require("./server");
const { openPrompter } = require("./prompt");
const { readJson, writeJson } = require("../functions/stores/files");

const LABEL = "com.broker.server";
const TARGET = `system/${LABEL}`;
const SERVICE_REL = [".local", "share", "broker-service"];
const SERVICE_DIR = path.join(os.homedir(), ...SERVICE_REL);
const PLIST = `/Library/LaunchDaemons/${LABEL}.plist`;

// DROP AFTER 2026-12. The service was registered as com.hltm.broker until
// September 2026. A renamed label does not replace the old daemon — it would sit
// beside it, KeepAlive holding the port — so an existing one is adopted rather
// than ignored. Its DATA directory is never moved: service.json records it by
// absolute path, and moving live tokens to make a name tidier is not worth it.
const LEGACY_LABEL = "com.hltm.broker";
const LEGACY_SERVICE_REL = [".local", "share", "hltm-broker-service"];
const LEGACY_SERVICE_DIR = path.join(os.homedir(), ...LEGACY_SERVICE_REL);
const LEGACY_PLIST = `/Library/LaunchDaemons/${LEGACY_LABEL}.plist`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function nodePath() {
  // Prefer the stable launcher on PATH (e.g. /opt/homebrew/bin/node) to the
  // versioned Cellar path process.execPath may resolve to.
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    const candidate = path.resolve(dir, "node");
    try { if (syncFs.realpathSync(candidate) === syncFs.realpathSync(process.execPath)) return candidate; }
    catch (_err) { /* next PATH entry */ }
  }
  return process.execPath;
}

function renderPlist(service) {
  const xml = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
  const template = syncFs.readFileSync(path.join(__dirname, "..", "launchd", `${LABEL}.plist`), "utf8");
  return template
    .replaceAll("REPLACE_USER", xml(service.user))
    .replaceAll("/REPLACE/ABSOLUTE/PATH/TO/node", xml(service.node))
    .replaceAll("/REPLACE/ABSOLUTE/PATH/TO/broker/cli.js", xml(path.join(service.runtime, "cli.js")))
    .replaceAll("/REPLACE/ABSOLUTE/DATA/DIR", xml(service.dataDir))
    .replace("<string>8787</string>", `<string>${service.port}</string>`);
}

async function healthy(service) {
  try {
    const cfg = await readJson(path.join(service.dataDir, "client.json"));
    const response = await fetch(`http://127.0.0.1:${service.port}/configGet`, {
      headers: { "x-broker-key": cfg.key }, signal: AbortSignal.timeout(1500)
    });
    return response.ok && (await response.json()).codex_handle_rollout === true;
  } catch (_err) { return false; }
}

// File operations are in the service user's own directories. Only registering
// the system LaunchDaemon crosses sudo; the broker process never runs as root.
// Dependencies are injectable so lifecycle tests do not touch real launchd.
function createServiceManager(deps = {}) {
  const root = deps.root || SERVICE_DIR;
  const plist = deps.plist || PLIST;
  const platform = deps.platform || process.platform;
  const user = deps.user || os.userInfo();
  const exec = deps.exec || execFileSync;
  const probe = deps.probe || healthy;
  const delay = deps.sleep || sleep;
  const alive = deps.alive || ((pid) => {
    try { process.kill(pid, 0); return true; }
    catch (err) { if (err.code === "ESRCH") return false; throw err; }
  });
  // DROP AFTER 2026-12. Off whenever the caller supplies its own root, so a
  // test with a temporary directory never reaches the real machine's old service.
  const legacyRoot = deps.legacyRoot || (deps.root ? null : LEGACY_SERVICE_DIR);
  const legacyPlist = deps.legacyPlist || (deps.plist ? null : LEGACY_PLIST);
  const metadata = path.join(root, "service.json");
  const runtime = path.join(root, "runtime");
  const backup = path.join(root, "runtime.previous");
  const descriptor = () => readJson(metadata);
  const exists = async (file) => { try { await fs.lstat(file); return true; } catch (err) { if (err.code === "ENOENT") return false; throw err; } };
  const sudo = (bin, args) => exec("/usr/bin/sudo", [bin, ...args], { stdio: "inherit" });

  function supported() {
    if (platform !== "darwin") throw new Error("automatic server service setup currently supports macOS; use broker serve with your supervisor on other systems");
    if (user.uid === 0) throw new Error("run as the service user, not sudo/root; sudo is requested only for LaunchDaemon registration");
  }
  function job() {
    try {
      const output = exec("/bin/launchctl", ["print", TARGET], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
      return { pid: Number(output.match(/^\s*pid = (\d+)/m)?.[1]) || null };
    } catch (err) {
      if (err.status === 113 || err.status === 3) return null; // service not loaded
      throw err;
    }
  }
  // DROP AFTER 2026-12.
  async function adoptLegacyService() {
    if (!legacyRoot && !legacyPlist) return false;
    if (await descriptor()) return false; // already installed under the new name
    const hadPlist = legacyPlist && await exists(legacyPlist);
    const hadRoot = legacyRoot && await exists(legacyRoot);
    if (!hadPlist && !hadRoot) return false;

    let running = null;
    try {
      const output = exec("/bin/launchctl", ["print", `system/${LEGACY_LABEL}`], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
      running = { pid: Number(output.match(/^\s*pid = (\d+)/m)?.[1]) || null };
    } catch (err) {
      if (!(err.status === 113 || err.status === 3)) throw err;
    }
    if (running) sudo("/bin/launchctl", ["bootout", `system/${LEGACY_LABEL}`]);
    if (hadPlist) sudo("/bin/rm", ["-f", legacyPlist]);
    if (hadRoot && !(await exists(root))) {
      await fs.mkdir(path.dirname(root), { recursive: true, mode: 0o700 });
      await fs.rename(legacyRoot, root);
      // service.json records absolute paths; the code directory just moved.
      const carried = await readJson(metadata);
      if (carried?.runtime) await writeJson(metadata, { ...carried, runtime });
    }
    return true;
  }

  async function requireInstalled() {
    supported();
    const service = await descriptor();
    if (!service) throw new Error("server is not installed; use install.sh --server first");
    if (service.user !== user.username || service.runtime !== runtime) throw new Error("server belongs to a different service installation");
    return service;
  }
  async function stop() {
    const running = job();
    if (!running) return;
    // Never kickstart -k: bootout sends SIGTERM and prevents KeepAlive from
    // restarting the old process while its replacement is being installed.
    sudo("/bin/launchctl", ["bootout", TARGET]);
    const deadline = Date.now() + (deps.stopTimeoutMs ?? 130_000);
    while (running.pid && alive(running.pid)) {
      if (Date.now() >= deadline) throw new Error("broker did not stop; runtime was not replaced (inspect the service before retrying)");
      await delay(250);
    }
  }
  function start() {
    sudo("/bin/launchctl", ["enable", TARGET]);
    sudo("/bin/launchctl", ["bootstrap", "system", plist]);
  }
  async function ready(service) {
    const deadline = Date.now() + (deps.startTimeoutMs ?? 20_000);
    do {
      if (job()?.pid && await probe(service)) return;
      await delay(250);
    } while (Date.now() < deadline);
    throw new Error(`broker did not become ready; inspect ${path.join(service.dataDir, "logs")}`);
  }
  function register(source) {
    exec("/usr/bin/plutil", ["-lint", source], { stdio: "ignore" });
    sudo("/usr/bin/install", ["-o", "root", "-g", "wheel", "-m", "644", source, plist]);
  }

  async function install(options = {}) {
    supported();
    await adoptLegacyService();
    const previous = await descriptor();
    if (previous) await requireInstalled();
    if (!previous && (await exists(plist) || await exists(runtime) || job())) throw new Error(`an unmanaged ${LABEL} service already exists; refusing to overwrite it`);
    if (previous && options.dataDir && path.resolve(options.dataDir) !== previous.dataDir) {
      throw new Error("changing the data directory is a migration, not an upgrade; existing accounts were left untouched");
    }
    const dataDir = path.resolve(previous?.dataDir || options.dataDir || DEFAULT_DATA_DIR);
    // Runtime/backup directories are replaced during upgrades; never store
    // account data anywhere inside that managed code directory.
    const relativeData = path.relative(path.resolve(root), dataDir);
    if (!relativeData || (!relativeData.startsWith(`..${path.sep}`) && relativeData !== ".." && !path.isAbsolute(relativeData))) {
      throw new Error("data directory must be outside the managed server code directory");
    }
    const port = Number(options.port ?? previous?.port ?? 8787);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("invalid server port");
    const source = await fs.realpath(options.from || path.join(__dirname, ".."));
    if (!(await exists(path.join(source, "lib", "server.js")))) throw new Error("source does not contain a self-hosted broker");
    const clientFile = path.join(dataDir, "client.json");
    const adminFile = path.join(dataDir, "admin.json");
    const oldClient = await readJson(clientFile);
    const oldAdmin = await readJson(adminFile);
    let url = options.url || previous?.url || oldClient?.url;
    const ui = (deps.openPrompter || openPrompter)(options.noAsk);
    try {
      if (!url) url = await ui?.ask("Broker URL for clients (private HTTPS, e.g. your Tailscale URL): ");
      if (!url) throw new Error("pass --url for a non-interactive server installation");
    } finally { ui?.close(); }
    const address = new URL(url);
    if (address.username || address.password || address.search || address.hash ||
        !(address.protocol === "https:" || (address.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(address.hostname)))) {
      throw new Error("use an HTTPS client URL (HTTP is allowed only on loopback)");
    }
    url = address.toString().replace(/\/+$/, "");
    const adminUrl = `http://127.0.0.1:${port}`;
    let settings = await readJson(path.join(dataDir, "settings.json"));
    if (!settings) {
      await init({ dataDir, url, adminUrl });
      settings = await readJson(path.join(dataDir, "settings.json"));
    }
    if (!settings.broker_key || !settings.client_key || !settings.codex_handle_secret) throw new Error("server settings are incomplete; refusing to replace existing secrets");
    await fs.mkdir(path.join(dataDir, "logs"), { recursive: true, mode: 0o700 });
    await fs.mkdir(root, { recursive: true, mode: 0o700 });
    const stage = await fs.mkdtemp(path.join(root, ".runtime-"));
    const candidatePlist = path.join(stage, `${LABEL}.plist`);
    const service = { user: user.username, runtime, dataDir, port, url, node: deps.node || nodePath() };
    let replaced = false;
    let stopped = false;
    let movedBackup = false;
    let startAttempted = false;
    try {
      // Install only server code, not credentials, .git, node_modules or the
      // global CLI symlink. This snapshot does not change on client upgrades.
      for (const name of ["cli.js", "package.json"]) await fs.copyFile(path.join(source, name), path.join(stage, name));
      for (const name of ["lib", "functions", "launchd"]) {
        await fs.cp(path.join(source, name), path.join(stage, name), {
          recursive: true,
          filter: (file) => !file.split(path.sep).includes("node_modules") &&
            (syncFs.statSync(file).isDirectory() || /\.(js|plist)$/.test(file))
        });
      }
      // Load the staged server before stopping a healthy old process.
      exec(service.node, ["-e", "require(process.argv[1])", path.join(stage, "lib", "server.js")], { stdio: "inherit" });
      await fs.writeFile(candidatePlist, renderPlist(service), { mode: 0o600 });
      exec("/usr/bin/plutil", ["-lint", candidatePlist], { stdio: "ignore" });
      if (previous) { await stop(); stopped = true; }
      if (await exists(runtime)) {
        if (!previous) throw new Error("unmanaged server runtime exists; refusing to overwrite it");
        await fs.rm(backup, { recursive: true, force: true });
        await fs.rename(runtime, backup);
        movedBackup = true;
      }
      await fs.rename(stage, runtime);
      replaced = true;
      // Even a first install interrupted by sudo/start failure remains repairable.
      if (!previous) await writeJson(metadata, service);
      // Derived connection files may change URL, but never mint new keys during
      // reinstall/upgrade. The token store itself is not copied or rolled back.
      await writeJson(clientFile, { ...oldClient, url, key: settings.client_key, role: "client" });
      await writeJson(adminFile, { ...oldAdmin, url: adminUrl, key: settings.broker_key, role: "admin" });
      const installedPlist = path.join(runtime, `${LABEL}.plist`);
      register(installedPlist);
      startAttempted = true;
      start();
      await ready(service);
      await writeJson(metadata, service);
      return { ...service, clientFile, adminFile, updated: Boolean(previous) };
    } catch (err) {
      if (previous && stopped) {
        try {
          if (startAttempted) await stop();
          if (replaced) await fs.rm(runtime, { recursive: true, force: true });
          if (movedBackup) await fs.rename(backup, runtime);
          if (oldClient) await writeJson(clientFile, oldClient);
          if (oldAdmin) await writeJson(adminFile, oldAdmin);
          const restoredPlist = path.join(runtime, `${LABEL}.plist`);
          await fs.writeFile(restoredPlist, renderPlist(previous), { mode: 0o600 });
          register(restoredPlist);
          start();
          await ready(previous);
        } catch (rollback) {
          throw new Error(`${err.message}; server recovery also failed: ${rollback.message}`);
        }
        throw new Error(`${err.message}; previous server runtime restored (tokens were not rolled back)`);
      }
      throw err;
    } finally {
      await fs.rm(stage, { recursive: true, force: true });
    }
  }

  async function restart() {
    const service = await requireInstalled();
    await stop();
    start();
    await ready(service);
    return service;
  }
  async function status() {
    const service = await requireInstalled();
    const running = job();
    return { ...service, pid: running?.pid || null, healthy: Boolean(running?.pid && await probe(service)) };
  }
  return { install, restart, status, requireInstalled, stop: async () => { await requireInstalled(); await stop(); } };
}

module.exports = {
  createServiceManager, renderPlist, healthy,
  SERVICE_DIR, SERVICE_REL, LEGACY_SERVICE_REL, LABEL
};
