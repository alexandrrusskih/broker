#!/usr/bin/env bash
set -euo pipefail

# One-shot installer for @pbl/broker, straight from the source repo.
# git is the only source: the npm registry copy trails this repo and would
# install an older CLI (and older wrappers) over a working setup.
#
#   curl -fsSL <raw-url>/install.sh | bash
# or just run this file.

REPO="${BROKER_REPO:-git@github.com:alexandrrusskih/broker.git}"
SUBDIR="${BROKER_SUBDIR:-}"
SRC="$HOME/.cache/broker/src"

SERVER=false
FROM=""
URL=""
DATA_DIR=""
PORT=""
CLIENT_CONFIG=""
NO_ASK=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER=true; shift ;;
    --no-ask) NO_ASK=true; shift ;;
    --url|--from|--data-dir|--port|--client-config)
      [ "$#" -ge 2 ] && [[ "$2" != --* ]] || { echo "error: $1 requires a value" >&2; exit 1; }
      case "$1" in
        --url) URL="$2" ;;
        --from) FROM="$2" ;;
        --data-dir) DATA_DIR="$2" ;;
        --port) PORT="$2" ;;
        --client-config) CLIENT_CONFIG="$2" ;;
      esac
      shift 2 ;;
    --help|-h)
      printf '%s\n' \
        'Usage: bash install.sh [--server] [--url <url>] [--no-ask] [--from <checkout>]' \
        'Client: optionally --client-config <file>; no daemon or shim is installed automatically.' \
        'Server: macOS system LaunchDaemon; optional --data-dir <dir> and --port <port>.' \
        'Without --from, installs upstream default branch. Setup never copies Codex auth or configures Tailscale.'
      exit 0 ;;
    *) echo "error: unknown option $1" >&2; exit 1 ;;
  esac
done

if $SERVER; then
  [ "$(uname -s)" = Darwin ] || { echo 'error: automatic server service setup supports macOS; use broker serve on other systems' >&2; exit 1; }
  [ "$(id -u)" -ne 0 ] || { echo 'error: run as the service user, not sudo/root; service registration requests sudo itself' >&2; exit 1; }
  [ -z "$CLIENT_CONFIG" ] || { echo 'error: --client-config is for clients, not --server' >&2; exit 1; }
elif [ -n "$DATA_DIR$PORT" ]; then
  echo 'error: --data-dir and --port require --server' >&2; exit 1
fi
command -v node >/dev/null 2>&1 || { echo 'error: Node >=20 is required' >&2; exit 1; }
node -e 'if (Number(process.versions.node.split(".")[0]) < 20) process.exit(1)' || { echo 'error: Node >=20 is required' >&2; exit 1; }

if [ -n "$FROM" ]; then
  PKG="$(cd "$FROM" && pwd -P)"
else
  if [ -d "$SRC/.git" ]; then
    if [ "$(git -C "$SRC" remote get-url origin)" != "$REPO" ]; then
      git -C "$SRC" remote set-url origin "$REPO"
    fi
    git -C "$SRC" fetch --depth 1 origin HEAD
    git -C "$SRC" reset --hard FETCH_HEAD
  else
    mkdir -p "$(dirname "$SRC")"
    git clone --depth 1 "$REPO" "$SRC"
  fi
  PKG="$SRC/${SUBDIR:+$SUBDIR}"
fi
[ -f "$PKG/package.json" ] || { echo "error: no package.json in $PKG" >&2; exit 1; }

if command -v bun >/dev/null 2>&1; then
  bun remove -g @pbl/broker >/dev/null 2>&1 || true
  bun install -g "$PKG"
elif command -v npm >/dev/null 2>&1; then
  npm remove -g @pbl/broker >/dev/null 2>&1 || true
  npm install -g "$PKG"
else
  echo "error: need bun or npm installed" >&2
  exit 1
fi

echo ""
echo "✓ @pbl/broker installed from $PKG"
if $SERVER; then SETUP_ARGS=(server install --from "$PKG"); else SETUP_ARGS=(setup); fi
[ -z "$URL" ] || SETUP_ARGS+=(--url "$URL")
$NO_ASK && SETUP_ARGS+=(--no-ask)
if $SERVER; then
  [ -z "$DATA_DIR" ] || SETUP_ARGS+=(--data-dir "$DATA_DIR")
  [ -z "$PORT" ] || SETUP_ARGS+=(--port "$PORT")
  node "$PKG/cli.js" "${SETUP_ARGS[@]}"
else
  [ -z "$CLIENT_CONFIG" ] || SETUP_ARGS+=(--client-config "$CLIENT_CONFIG")
  # Invoking the new source directly avoids an older global `broker` earlier on PATH.
  node "$PKG/cli.js" "${SETUP_ARGS[@]}"
  echo "  next:"
  echo "    broker codex install   # wrapper + shim; configure the connection first"
  echo "    broker agy install     # same for agy"
  echo "    broker deploy --project <your-firebase-project> --dedicated-project  # optional Firebase server"
fi
