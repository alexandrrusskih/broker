const fs = require("fs");
const os = require("os");
const path = require("path");
const config = require("./config");
const { WRAP, binDirFor } = require("./wrap");

// The marker the wrapper looks for when it resolves the real binary, so a shim
// is never mistaken for the harness itself (that would recurse forever).
const MARK = "hltm-broker shim";
function shimPath(provider, binDir) {
  return path.join(binDirFor(binDir), WRAP[provider].bin);
}

function isShim(file) {
  try {
    return fs.readFileSync(file, "utf8").slice(0, 512).includes(MARK);
  } catch (_e) {
    return false;
  }
}

// Where a real binary goes when the shim takes its name. codex and claude put a
// symlink in ~/.local/bin and keep the program elsewhere, so taking the name
// costs nothing. agy IS the program at that path — 178 MB of it — and writing
// the shim over it would destroy the only copy, with nothing left to restore.
function stashDir(binDir) {
  return path.join(binDirFor(binDir), "..", "lib", "hltm-broker", "real");
}

// What the native name pointed at before we took it over, so `remove` can put
// the original back instead of guessing.
function rememberOriginal(provider, binDir) {
  const cfg = config.read();
  const previous = cfg.shim_previous || {};
  const target = shimPath(provider, binDir);
  if (isShim(target) || !fs.existsSync(target)) return previous[provider] || null;

  const stat = fs.lstatSync(target);
  if (stat.isSymbolicLink()) {
    // Keep the link's own target, not its resolved path: it usually points at a
    // `current` symlink the updater moves, and resolving would pin a version
    // that gets deleted on the next update — leaving a dangling restore.
    previous[provider] = fs.readlinkSync(target);
  } else {
    // A real file: move it aside first, then let the shim take the name.
    const stash = path.join(stashDir(binDir), WRAP[provider].bin);
    fs.mkdirSync(path.dirname(stash), { recursive: true });
    fs.renameSync(target, stash);
    previous[provider] = stash;
  }
  config.write({ shim_previous: previous });
  return previous[provider];
}

function install(provider, binDir) {
  const w = WRAP[provider];
  const wrapper = path.join(binDirFor(binDir), w.cmd);
  if (!fs.existsSync(wrapper)) {
    throw new Error(`${w.cmd} is not installed — run 'broker ${provider} install' first`);
  }

  const target = shimPath(provider, binDir);
  rememberOriginal(provider, binDir);

  const body = `#!/bin/sh
# ${MARK}: \`${w.bin}\` goes through ${w.cmd}, so the broker picks a live account
# even for tools that only know the native command name.
# Undo with: broker ${provider} remove
exec ${wrapper} "$@"
`;
  const tmp = `${target}.hltm-${process.pid}`;
  fs.writeFileSync(tmp, body, { mode: 0o755 });
  fs.renameSync(tmp, target); // atomic: never a window where the command is missing

  const shims = new Set(config.read().shims || []);
  shims.add(provider);
  config.write({ shims: [...shims].sort() });
  return { target, wrapper };
}

function remove(provider, binDir) {
  const target = shimPath(provider, binDir);
  if (!isShim(target)) return { removed: false, target };

  const cfg = config.read();
  const original = (cfg.shim_previous || {})[provider];
  if (!original || !fs.existsSync(original)) {
    throw new Error(`cannot find the real ${WRAP[provider].bin} to restore — put it back by hand`);
  }
  fs.unlinkSync(target);
  // A stashed binary goes back where it came from; a remembered symlink target is
  // relinked. Restoring a moved file as a symlink into our own lib directory would
  // leave the harness dependent on the broker it was just detached from.
  if (path.dirname(original) === stashDir(binDir)) fs.renameSync(original, target);
  else fs.symlinkSync(original, target);
  config.write({ shims: (cfg.shims || []).filter((p) => p !== provider) });
  return { removed: true, target, original };
}

// Reinstall only if it was installed before and something (an updater, usually)
// has since taken the native name back.
function ensure(provider, binDir) {
  const cfg = config.read();
  if (!(cfg.shims || []).includes(provider)) return false;
  if (isShim(shimPath(provider, binDir))) return false;
  install(provider, binDir);
  return true;
}

// What the shell would actually run for the native name. A shim only works if it
// is the FIRST match in PATH — a copy installed by nvm or homebrew earlier in
// PATH wins silently, and the bare command goes around the broker.
// The wrapper puts a directory of its own first in PATH before handing over, so
// the harness re-invoking itself by name reaches the real binary instead of the
// shim. Inside such a session that directory IS the first match — reporting it
// as "something shadows your shim" would be us complaining about ourselves.
const SHIM_FREE = /hltm-real-[a-z]+-\d+$/;

function resolveInPath(bin) {
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir || SHIM_FREE.test(dir)) continue;
    const candidate = path.join(dir, bin);
    try {
      if (fs.statSync(candidate).isFile()) {
        fs.accessSync(candidate, fs.constants.X_OK);
        return candidate;
      }
    } catch (_e) {
      // not there, or not executable — keep looking
    }
  }
  return null;
}

function status(provider, binDir) {
  const w = WRAP[provider];
  const target = shimPath(provider, binDir);
  const wrapper = path.join(binDirFor(binDir), w.cmd);
  const declared = (config.read().shims || []).includes(provider);
  const shimmed = isShim(target);
  const resolved = resolveInPath(w.bin);
  // Shimmed, but the shell finds a different file first. Worse than not shimming
  // at all, because status used to report a green line while the bare command ran
  // an unbrokered binary — which then rotates the account's token and breaks the
  // broker's copy and invalidate the shared grant.
  const shadowedBy =
    shimmed && resolved && path.resolve(resolved) !== path.resolve(target) && !isShim(resolved)
      ? resolved
      : null;
  return {
    cmd: w.cmd,
    bin: w.bin,
    wrapper: fs.existsSync(wrapper) ? wrapper : null,
    native: target,
    shimmed,
    resolved,
    shadowedBy,
    declared,
    broken: declared && !shimmed // an updater took the name back
  };
}

module.exports = { install, remove, ensure, status, isShim, shimPath, MARK };
