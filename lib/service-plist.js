// What a LaunchDaemon for the broker looks like: where its files go, the plist
// it is registered from, and the one call that says whether it answers.
const syncFs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { readJson } = require("../functions/stores/files");

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

module.exports = {
  nodePath, renderPlist, healthy, sleep,
  LABEL, TARGET, SERVICE_DIR, SERVICE_REL, PLIST,
  LEGACY_LABEL, LEGACY_SERVICE_DIR, LEGACY_SERVICE_REL, LEGACY_PLIST,
};
