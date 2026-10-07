// The shell a plain wrapper is made of, and the revision stamp it carries.
const path = require("path");
const { execFileSync } = require("child_process");
const config = require("./config");


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
# ${w.cmd}: ${w.bin} via the broker (the broker is the sole refresh authority).
# Account: $${envVar} > $BROKER_ACCOUNT >${pinnedNote(pinned)} the broker config > "default".
${w.homeEnv ? `# $${w.homeEnv} relocates the auth file, giving each account its own profile.\n` : ""}set -e
umask 077
CFG="$HOME/${config.CONFIG_REL}"
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


function pinnedNote(account) {
  return account ? ` ${JSON.stringify(account)} (pinned at wrap time) >` : "";
}

module.exports = { accountVar, accountScript, shellWrapper, stamp, pinnedNote };
