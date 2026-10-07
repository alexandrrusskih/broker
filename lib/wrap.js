// Installing a wrapper: where it goes, and which of the two shapes it takes.
//
// Each wrapper refreshes the provider's auth file from the broker, then execs
// the real CLI. The broker stays the SOLE refresh authority — the bare CLI must
// not be used directly under that account, or the rotation race returns.
const fs = require("fs");
const os = require("os");
const path = require("path");
const config = require("./config");
const { WRAP } = require("./wrap-table");
const { shellWrapper } = require("./wrap-script");
const engine = require("./wrap-engine");

// Where the wrapper and shim go. An image build usually wants /usr/local/bin:
// a shim only shadows the harness when its directory comes first in PATH, and in
// a container the harness itself lives there.
function binDirFor(override) {
  if (override) return path.resolve(override);
  // Where a previous install put things — an image build uses /usr/local/bin, and
  // status/ensure must look there rather than guessing the default again.
  const remembered = config.read().bin_dir;
  return remembered ? path.resolve(remembered) : path.join(os.homedir(), ".local", "bin");
}

function install(provider, account, binDirOverride, options = {}) {
  const w = WRAP[provider];
  if (!w) throw new Error(`unknown provider: ${provider} (codex|claude|agy)`);
  // A key is not needed to lay the wrapper down — an image build has no
  // secrets, and the key arrives at run time (BROKER_KEY). Say so once instead
  // of refusing to install.
  if (!config.read().key) {
    console.warn("  no broker key yet — set one at run time with 'broker config --key <broker_key>'");
  }

  const binDir = binDirFor(binDirOverride);
  fs.mkdirSync(binDir, { recursive: true });
  if (binDirOverride) config.write({ bin_dir: binDir });
  const target = path.join(binDir, w.cmd);

  // The codex wrapper takes no account at install time: it is named per run or
  // picked per run, and nothing in between.
  const script = w.template
    ? engine.templateWrapper(provider, w, binDirOverride ? engine.pkgDirFor(binDirOverride) : null,
                             options.container === true)
    : shellWrapper(w, provider, account);
  fs.writeFileSync(target, script, { mode: 0o755 });

  return {
    cmd: w.cmd,
    path: target,
    pinned: w.template ? null : account || null,
    inPath: (process.env.PATH || "").split(":").includes(binDir)
  };
}

// The public surface of wrapping, whichever file holds the code.
module.exports = {
  install, WRAP,
  installContainer: engine.installContainer,
  binDirFor,
  pkgDirFor: engine.pkgDirFor,
  pythonForLauncher: engine.pythonForLauncher,
};
