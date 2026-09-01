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
SRC="$HOME/.cache/hltm-broker/src"

if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch --depth 1 origin HEAD
  git -C "$SRC" reset --hard FETCH_HEAD
else
  mkdir -p "$(dirname "$SRC")"
  git clone --depth 1 "$REPO" "$SRC"
fi

PKG="$SRC/${SUBDIR:+$SUBDIR}"
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
echo "  next:"
echo "    broker deploy --project <your-firebase-project>"
echo "    broker codex install   # wrapper + shim: plain 'codex' goes through the broker"
echo "    broker agy install     # same for agy"
