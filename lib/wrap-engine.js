// Putting the python engine where a launcher can find it: beside the wrapper
// on this machine, and zipped into one file for medulla's container.
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");
const { WRAP } = require("./wrap-table");
const { stamp } = require("./wrap-script");


// codex ships a real wrapper instead: it picks the account with the most
// rate-limit headroom, so a bare `cx` never lands on an exhausted one.
// The engine is a package, not a script: `cx` is a launcher that points at it.
// Install it beside the binaries so a fix is one file in the package rather than
// a rewrite of a single huge wrapper.
const PKG_DIR = path.join(os.homedir(), ".local", "lib", "broker");

// The engine sits next to the wrapper: with --bin-dir /usr/local/bin it lands in
// /usr/local/lib, which every user of the image can read. Installing under the
// building user's $HOME would leave it unreachable once the image drops to its
// runtime user.
function pkgDirFor(binDirOverride) {
  return binDirOverride
    ? path.join(path.resolve(binDirOverride), "..", "lib", "broker")
    : PKG_DIR;
}

function installEngine(pkgDir) {
  const src = path.join(__dirname, "wrappers", "broker");
  const root = pkgDir || PKG_DIR;
  const dst = path.join(root, "broker");
  require("./legacy").claimEngineDir(root); // DROP AFTER 2026-12
  // Replace wholesale: a stale module left behind by an older version would keep
  // being imported and shadow the new layout.
  fs.rmSync(dst, { recursive: true, force: true });
  fs.mkdirSync(root, { recursive: true });
  fs.cpSync(src, dst, {
    recursive: true,
    filter: (from) => !from.includes("__pycache__")
  });
  fs.writeFileSync(
    path.join(dst, "version.py"),
    `"""Stamped by \`broker wrap\` — what this install was cut from."""

STAMP = ${JSON.stringify(stamp())}
`
  );
  return dst;
}

// medulla mounts private tooling into its container one file at a time and skips
// symlinks to directories (its Dockerfile says a private wrapper cannot live in a
// public image, so it comes from the host). A launcher plus a package would need
// a mount per module — and a new module would go missing unnoticed. So the engine
// is zipped into ONE executable, with the native name as a shim beside it.
function refreshContainerOverlay(provider, engineDir, create = false,
  overlayRoot = path.join(os.homedir(), ".medulla", "container")) {
  const w = WRAP[provider];
  const overlayBin = path.join(overlayRoot, "bin");
  // Existing overlays still update as before. A first overlay is opt-in: the
  // presence of ~/.medulla alone does not authorize intercepting container CLIs.
  if (!create && !fs.existsSync(path.dirname(overlayBin))) return null;
  fs.mkdirSync(overlayBin, { recursive: true });

  // Leftover from the previous approach, when the package was mounted piecemeal.
  // Both spellings: DROP AFTER 2026-12 takes the hltm- one with it.
  for (const name of ["broker", "hltm-broker"]) {
    fs.rmSync(path.join(overlayRoot, "home", ".local", "lib", name), {
      recursive: true,
      force: true
    });
  }

  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "broker-bundle-"));
  const bundle = path.join(overlayBin, w.cmd);
  try {
    fs.cpSync(engineDir, path.join(staging, "broker"), {
      recursive: true,
      filter: (from) => !from.includes("__pycache__")
    });
    const entry = fs
      .readFileSync(path.join(__dirname, "wrappers", "bundle_main.py"), "utf8")
      .replace("__PROVIDER__", provider);
    fs.writeFileSync(path.join(staging, "__main__.py"), entry);
    execFileSync("python3", ["-m", "zipapp", staging, "-o", bundle, "-p", "/usr/bin/env python3"], {
      stdio: "ignore"
    });
    fs.chmodSync(bundle, 0o755);
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }

  // The native name inside the container points at the bundle, so a workflow
  // calling plain `codex` goes through the broker with no per-workflow setting.
  //
  // WHERE it goes matters more than it looks. Docker resolves a symlink before
  // bind-mounting over it, so a shim mounted at /usr/local/bin/claude — which the
  // image ships as a symlink into node_modules — lands ON the 339 MB binary and
  // destroys the only copy in the container. Mounting into the container HOME's
  // .local/bin instead shadows the name through PATH (it comes first) and leaves
  // every real file untouched. A provider whose binary already lives there gets
  // no container shim at all, for the same reason.
  const homeShims = path.join(overlayRoot, "home");
  const legacy = path.join(overlayBin, w.bin);
  if (fs.existsSync(legacy)) fs.rmSync(legacy, { force: true });
  if (w.containerShim) {
    const dir = path.join(homeShims, w.containerShim);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, w.bin),
      ["#!/bin/sh", `# broker shim: ${w.bin} goes through ${w.cmd} inside the container.`, `exec /usr/local/bin/${w.cmd} "$@"`, ""].join("\n"),
      { mode: 0o755 }
    );
  }
  return bundle;
}

function installContainer(provider, overlayRoot) {
  if (!WRAP[provider]?.template) throw new Error(`no container wrapper for ${provider}`);
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "broker-container-"));
  try {
    const engineDir = installEngine(path.join(staging, "engine"));
    return refreshContainerOverlay(provider, engineDir, true, overlayRoot);
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
}

// Which python the launcher should name, by absolute path.
//
// `#!/usr/bin/env python3` resolves against the PATH of whoever runs it, and that
// is not always a shell someone typed in. A workflow that prepares a subprocess
// with PATH=/usr/bin:/bin got macOS's own python3 — 3.9 — and every wrapper died
// with "cannot find the broker engine (No module named 'tomllib')", which names
// neither the real problem nor a fix that works. The engine needs 3.11 for TOML,
// so the install picks an interpreter that HAS it and writes that path down.
//
// Tested rather than assumed: a version number says less than the import itself.
function pythonForLauncher() {
  const candidates = [];
  try {
    candidates.push(execFileSync("sh", ["-c", "command -v python3"],
                                 { encoding: "utf8" }).trim());
  } catch (error) { /* no python3 on PATH: the fixed paths below may still have one */ }
  candidates.push("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3");
  const tried = [];
  for (const candidate of candidates) {
    if (!candidate || tried.includes(candidate) || !fs.existsSync(candidate)) continue;
    tried.push(candidate);
    try {
      execFileSync(candidate, ["-c", "import tomllib"], { stdio: "ignore" });
      return candidate;
    } catch (error) { /* older than 3.11 — keep looking */ }
  }
  return null;
}

function templateWrapper(provider, w, pkgDir, container = false) {
  const engineDir = installEngine(pkgDir);
  // Only the host install feeds medulla's container overlay; an image build
  // (--bin-dir) has no overlay to refresh.
  if (!pkgDir) refreshContainerOverlay(provider, engineDir, container);
  const src = fs.readFileSync(path.join(__dirname, "wrappers", w.template), "utf8");
  if (!src.includes("__PKG_DIR__")) {
    throw new Error(`launcher ${w.template} lost its __PKG_DIR__ marker`);
  }
  let script = src.replace("__PKG_DIR__", path.dirname(engineDir));
  // Only for a launcher that runs on THIS machine. An image build lays the same
  // file down for a container, where a host path means nothing — there
  // `env python3` is the only honest answer.
  if (!pkgDir && !container) {
    const python = pythonForLauncher();
    if (python) script = script.replace(/^#!.*\n/, `#!${python}\n`);
    else console.warn("  no python3 with tomllib found — the wrapper keeps 'env python3'");
  }
  return script;
}

module.exports = { PKG_DIR, pkgDirFor, installEngine, refreshContainerOverlay,
                   installContainer, pythonForLauncher, templateWrapper };
