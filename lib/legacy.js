// DROP AFTER 2026-12 — this whole file, and every call to it.
//
// The tool was called hltm-broker until September 2026. What it left on disk is
// carried across exactly once, here, rather than being checked on every read:
// a migration belongs in `install`/`upgrade`, which is when a machine is being
// changed anyway. Covered by test/legacy-paths.test.js.
const fs = require("fs");
const os = require("os");
const path = require("path");
const config = require("./config");

// The engine directory was ~/.local/lib/hltm-broker, and `real/` under it holds
// binaries the shims moved aside — including agy, which IS its 180 MB program
// and exists nowhere else. Renaming without carrying that across would leave the
// harness unfindable.
function claimEngineDir(root) {
  const legacy = path.join(path.dirname(root), "hltm-broker");
  const from = path.join(legacy, "real");
  const to = path.join(root, "real");
  try {
    if (!fs.existsSync(from) || fs.existsSync(to)) return null;
    fs.mkdirSync(root, { recursive: true });
    fs.renameSync(from, to);
    fs.rmSync(legacy, { recursive: true, force: true });
    // `shim_previous` remembers where each stashed binary went, by absolute
    // path. Move the files and leave those pointing at the old directory, and
    // `broker <provider> remove` refuses to restore anything.
    const previous = config.read().shim_previous || {};
    const moved = Object.fromEntries(
      Object.entries(previous).map(([name, was]) =>
        [name, typeof was === "string" && was.startsWith(from + path.sep)
          ? path.join(to, path.relative(from, was))
          : was])
    );
    config.write({ shim_previous: moved });
    return to;
  } catch (_e) {
    // a cross-device rename, or no permission — the caller still installs
    return null;
  }
}

// The config itself stays where it is: an older wrapper still on this machine
// reads it. The credential cache beside it does not — it holds access tokens, it
// is rebuilt on demand, and tokens nothing reads are tokens sitting on disk for
// no reason. Only once the current config exists, so nothing is dropped from an
// installation that has not moved yet.
function dropCaches() {
  const home = os.homedir();
  if (!fs.existsSync(config.FILE)) return false;
  fs.rmSync(path.join(home, ".config", "hltm-broker", "cache"), { recursive: true, force: true });
  // Only a checkout cache — re-cloned on demand.
  fs.rmSync(path.join(home, ".cache", "hltm-broker"), { recursive: true, force: true });
  return true;
}

module.exports = { claimEngineDir, dropCaches };
