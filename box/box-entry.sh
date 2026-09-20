#!/bin/sh
# Start what this box was asked to provide, then become the harness — as you.
#
# A box that runs containers has to start its daemon as root, but the harness
# itself must run as your own uid or every file it writes into the project comes
# back owned by root. So: root only long enough to start the daemon, then drop.
# A box without docker never runs as root at all (see box.py).
set -e

if [ "${BROKER_BOX_DOCKER:-}" = "1" ] && ! docker info >/dev/null 2>&1; then
  dockerd --host=unix:///var/run/docker.sock >/var/log/dockerd.log 2>&1 &
  waited=0
  while ! docker info >/dev/null 2>&1; do
    waited=$((waited + 1))
    if [ "$waited" -gt 60 ]; then
      echo "broker box: the docker daemon did not start; see /var/log/dockerd.log" >&2
      break
    fi
    sleep 0.5
  done
  # Yours to use without sudo, which nothing in here has anyway.
  chgrp "${BROKER_BOX_GID:-0}" /var/run/docker.sock 2>/dev/null || true
  chmod 0660 /var/run/docker.sock 2>/dev/null || true
fi

if [ -n "${BROKER_BOX_UID:-}" ] && [ "$(id -u)" = "0" ]; then
  exec setpriv --reuid "$BROKER_BOX_UID" --regid "${BROKER_BOX_GID:-0}" --clear-groups "$@"
fi
exec "$@"
