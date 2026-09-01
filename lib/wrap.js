const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");
const config = require("./config");

// Each wrapper refreshes the provider's auth file from the broker, then execs
// the real CLI. The broker stays the SOLE refresh authority — the bare CLI must
// not be used directly under that account, or the rotation race returns.
//
// homeEnv is the provider's own "config dir" variable: setting it gives an
// account its own profile, so two accounts never fight over one auth file.
const WRAP = {
  codex: { cmd: "broker-cx", bin: "codex", authdir: ".codex", authname: "auth.json", homeEnv: "CODEX_HOME", format: "authjson", template: "codex.py", profileBase: ".codex", authRel: "auth.json", containerShim: ".local/bin" },
  // claude takes its token through CLAUDE_CODE_OAUTH_TOKEN, so it needs no
  // profile and nothing in ~/.claude splits per account — see providers/claude.py.
  claude: { cmd: "broker-cl", bin: "claude", authdir: ".claude", authname: ".credentials.json", homeEnv: "CLAUDE_CONFIG_DIR", format: "authjson", template: "claude.py", profileBase: null, authRel: null, containerShim: ".local/bin" },
  // agy keeps its token three levels inside $HOME and has no config-dir variable
  // of its own, so its profile IS a home directory (see providers/agy.py). The
  // engine handles that; the entries here only describe the launcher.
  agy: { cmd: "broker-agy", bin: "agy", authdir: ".gemini/antigravity-cli", authname: "antigravity-oauth-token", homeEnv: "HOME", format: "authjson", template: "agy.py", profileBase: ".agy", authRel: ".gemini/antigravity-cli/antigravity-oauth-token", containerShim: null }
};

// The wrapper resolves its account at RUN time, so `broker set-default <name>`
// takes effect without re-wrapping. An account pinned at wrap time (explicit
// `--account`) sits between the env override and the config default.
// The wrapper's own account variable. It is built from the PROVIDER, not the
// command: `broker-cl`.toUpperCase() is BROKER-CL, and `${BROKER-CL_ACCOUNT:-x}`
// is not a variable reference at all — sh parses it as ${BROKER-…}, "use $BROKER
// or this default", so the account silently became the literal `CL_ACCOUNT:-`.
// CLAUDE_ACCOUNT also matches what the python engine and CI already use.
function accountVar(provider) {
  return `${provider.toUpperCase().replace(/[^A-Z0-9]/g, "_")}_ACCOUNT`;
}

function accountScript(w, provider, pinned) {
  const envVar = accountVar(provider);
  const lines = [
    `ACCOUNT="\${${envVar}:-\${BROKER_ACCOUNT:-}}"`
  ];
  if (pinned) lines.push(`[ -n "$ACCOUNT" ] || ACCOUNT=${JSON.stringify(pinned)}`);
  lines.push(
    `[ -n "$ACCOUNT" ] || ACCOUNT=$(python3 -c "import json;print(json.load(open('$CFG')).get('account') or '')")`,
    `ACCOUNT="\${ACCOUNT:-default}"`
  );
  return lines.join("\n");
}

// Providers without their own template get the plain shell wrapper: fetch the
// auth file, exec the CLI. The token lands atomically and 0600 — a dropped
// connection must not leave a half-written file where a working one was.
function shellWrapper(w, provider, pinned) {
  const envVar = accountVar(provider);
  const authDir = w.homeEnv ? `"\${${w.homeEnv}:-$HOME/${w.authdir}}"` : `"$HOME/${w.authdir}"`;
  return `#!/bin/sh
# ${w.cmd}: ${w.bin} via the hltm token-broker (broker is the sole refresh authority).
# Account: $${envVar} > $BROKER_ACCOUNT >${pinnedNote(pinned)} the broker config > "default".
${w.homeEnv ? `# $${w.homeEnv} relocates the auth file, giving each account its own profile.\n` : ""}set -e
umask 077
CFG="$HOME/.config/hltm-broker/config.json"
[ -f "$CFG" ] || { echo "${w.cmd}: broker not configured — run 'broker config --key <broker_key>'" >&2; exit 1; }
URL=$(python3 -c "import json;print(json.load(open('$CFG'))['url'])")
KEY=$(python3 -c "import json;print(json.load(open('$CFG'))['key'])")
${accountScript(w, provider, pinned)}
ACCOUNT_ENC=$(python3 -c "import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=''))" "$ACCOUNT")
DIR=${authDir}
mkdir -p "$DIR"
TMP="$DIR/.${w.authname}.$$"
trap 'rm -f "$TMP"' EXIT INT TERM
curl -fsS --connect-timeout 10 --max-time 30 -H "x-broker-key: $KEY" \\
  "$URL/getToken?provider=${provider}&account=$ACCOUNT_ENC&format=${w.format}" -o "$TMP"
mv -f "$TMP" "$DIR/${w.authname}"
exec ${w.bin} "$@"
`;
}

// Which revision this wrapper was cut from, so `cx version` can say whether it
// is behind the repo. Absent when installed from a published tarball (no .git).
function stamp() {
  const root = path.join(__dirname, "..");
  const git = (args) => execFileSync("git", ["-C", root, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  const version = { pkg: require("../package.json").version };
  try {
    version.commit = git(["rev-parse", "--short", "HEAD"]);
    version.date = git(["log", "-1", "--format=%cs"]);
    // Built from a tree with uncommitted changes — say so, or `cx version`
    // would claim to be a revision that does not contain what is installed.
    if (git(["status", "--porcelain"])) version.commit += "+dirty";
  } catch (_e) {
    // not a checkout — the package version is all we can report
  }
  return version;
}

// codex ships a real wrapper instead: it picks the account with the most
// rate-limit headroom, so a bare `cx` never lands on an exhausted one.
// The engine is a package, not a script: `cx` is a launcher that points at it.
// Install it beside the binaries so a fix is one file in the package rather than
// a rewrite of a single huge wrapper.
const PKG_DIR = path.join(os.homedir(), ".local", "lib", "hltm-broker");

// The engine sits next to the wrapper: with --bin-dir /usr/local/bin it lands in
// /usr/local/lib, which every user of the image can read. Installing under the
// building user's $HOME would leave it unreachable once the image drops to its
// runtime user.
function pkgDirFor(binDirOverride) {
  return binDirOverride
    ? path.join(path.resolve(binDirOverride), "..", "lib", "hltm-broker")
    : PKG_DIR;
}

function installEngine(pkgDir) {
  const src = path.join(__dirname, "wrappers", "hltm");
  const root = pkgDir || PKG_DIR;
  const dst = path.join(root, "hltm");
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
function refreshContainerOverlay(provider, engineDir) {
  const w = WRAP[provider];
  const overlayBin = path.join(os.homedir(), ".medulla", "container", "bin");
  if (!fs.existsSync(path.dirname(overlayBin))) return null; // no medulla here
  fs.mkdirSync(overlayBin, { recursive: true });

  // Leftover from the previous approach, when the package was mounted piecemeal.
  fs.rmSync(path.join(os.homedir(), ".medulla", "container", "home", ".local", "lib", "hltm-broker"), {
    recursive: true,
    force: true
  });

  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "hltm-bundle-"));
  const bundle = path.join(overlayBin, w.cmd);
  try {
    fs.cpSync(engineDir, path.join(staging, "hltm"), {
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
  const homeShims = path.join(os.homedir(), ".medulla", "container", "home");
  const legacy = path.join(overlayBin, w.bin);
  if (fs.existsSync(legacy)) fs.rmSync(legacy, { force: true });
  if (w.containerShim) {
    const dir = path.join(homeShims, w.containerShim);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, w.bin),
      ["#!/bin/sh", `# hltm-broker shim: ${w.bin} goes through ${w.cmd} inside the container.`, `exec /usr/local/bin/${w.cmd} "$@"`, ""].join("\n"),
      { mode: 0o755 }
    );
  }
  return bundle;
}

function templateWrapper(provider, w, pkgDir) {
  const engineDir = installEngine(pkgDir);
  // Only the host install feeds medulla's container overlay; an image build
  // (--bin-dir) has no overlay to refresh.
  if (!pkgDir) refreshContainerOverlay(provider, engineDir);
  const src = fs.readFileSync(path.join(__dirname, "wrappers", w.template), "utf8");
  if (!src.includes("__PKG_DIR__")) {
    throw new Error(`launcher ${w.template} lost its __PKG_DIR__ marker`);
  }
  return src.replace("__PKG_DIR__", path.dirname(engineDir));
}

// Names these wrappers used to carry. `cx`/`cl`/`gm` are short but not ours to
// claim — `cx` is Cloud 66's CLI, `gm` is GraphicsMagick — so they were renamed
// with a broker- prefix. Clean up the old file, but only when it is ours.
const LEGACY_NAMES = { codex: ["cx"], claude: ["cl"], agy: ["gm", "broker-gm"] };

function dropLegacyNames(provider, binDir) {
  const dropped = [];
  for (const name of LEGACY_NAMES[provider] || []) {
    const old = path.join(binDir, name);
    try {
      if (!fs.existsSync(old)) continue;
      const head = fs.readFileSync(old, "utf8").slice(0, 800);
      if (!head.includes("hltm")) continue; // someone else's binary, leave it
      fs.unlinkSync(old);
      dropped.push(old);
    } catch (_e) {
      // unreadable (a real binary, most likely) — not ours to remove
    }
  }
  return dropped;
}

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

function install(provider, account, binDirOverride) {
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
    ? templateWrapper(provider, w, binDirOverride ? pkgDirFor(binDirOverride) : null)
    : shellWrapper(w, provider, account);
  fs.writeFileSync(target, script, { mode: 0o755 });
  const dropped = dropLegacyNames(provider, binDir);

  return {
    cmd: w.cmd,
    path: target,
    pinned: w.template ? null : account || null,
    dropped,
    inPath: (process.env.PATH || "").split(":").includes(binDir)
  };
}

function pinnedNote(account) {
  return account ? ` ${JSON.stringify(account)} (pinned at wrap time) >` : "";
}

module.exports = { install, binDirFor, WRAP };
