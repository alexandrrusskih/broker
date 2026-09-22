#!/bin/sh
# Start what this box was asked to provide, then become the harness — as you.
#
# A box that runs containers has to start its daemon as root, but the harness
# itself must run as your own uid or every file it writes into the project comes
# back owned by root. So: root only long enough to start the daemon, then drop.
# A box without docker never runs as root at all (see box.py).
set -e

# The daemon is started on demand, not at startup. Every box that MIGHT run
# tests used to raise one, and most windows never do: a dozen open boxes meant a
# dozen daemons, each holding memory, and each delaying the harness by up to
# thirty seconds while the box waited for it to come up.
#
# So the root shell leaves a listener behind and drops to you. The first call to
# `docker` — the wrapper below — asks for the daemon and waits for it; every
# call after that finds it already running. A box that only ever reads code
# never raises one at all.
if [ "${BROKER_BOX_DOCKER:-}" = "1" ] && ! docker info >/dev/null 2>&1; then
  rm -f /run/broker-docker-start
  mkfifo -m 0622 /run/broker-docker-start 2>/dev/null || true

  # A blocking read on a fifo costs nothing while nobody writes to it — no
  # polling loop, no wakeups.
  (
    while read -r _ < /run/broker-docker-start; do
      docker info >/dev/null 2>&1 && continue
      # Images come from the machine's own pull-through cache when there is
      # one: the first box to want Postgres downloads it, every box after that
      # copies it over the loopback. It also steps around a resolver that
      # occasionally times out on registry-1.docker.io and takes a deploy with
      # it. Plain http, and only to this machine — the cache is bound to its
      # loopback and serves public images.
      mirror=""
      if [ -n "${BROKER_BOX_REGISTRY_MIRROR:-}" ]; then
        mirror="--registry-mirror=${BROKER_BOX_REGISTRY_MIRROR} --insecure-registry=${BROKER_BOX_REGISTRY_MIRROR#http://}"
      fi
      # shellcheck disable=SC2086
      dockerd --host=unix:///var/run/docker.sock $mirror >/var/log/dockerd.log 2>&1 &
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
    done
  ) &

  # The private network, raised the same way and for the same reason: most
  # boxes never touch it, and a daemon that joined on startup would put every
  # open window on the network as a member of its own. This way a box appears
  # there only if something in it actually goes looking.
  #
  # The daemon needs root and a tun device, so the same root listener raises it;
  # the wrapper below only asks. Authorisation is kept in /var/lib/tailscale,
  # mounted from outside, so a box joins once rather than on every run.
  if [ -x /usr/local/sbin/tailscaled ]; then
    rm -f /run/broker-tailscale-start
    mkfifo -m 0622 /run/broker-tailscale-start 2>/dev/null || true
    mkdir -p /var/lib/tailscale /var/run/tailscale
    chgrp "${BROKER_BOX_GID:-0}" /var/run/tailscale 2>/dev/null || true
    chmod 0770 /var/run/tailscale 2>/dev/null || true
    (
      while read -r _ < /run/broker-tailscale-start; do
        pgrep -x tailscaled >/dev/null 2>&1 && continue
        /usr/local/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state \
          --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 &
        waited=0
        while [ ! -S /var/run/tailscale/tailscaled.sock ]; do
          waited=$((waited + 1))
          if [ "$waited" -gt 40 ]; then
            echo "broker box: tailscaled did not start; see /var/log/tailscaled.log" >&2
            break
          fi
          sleep 0.25
        done
        chgrp "${BROKER_BOX_GID:-0}" /var/run/tailscale/tailscaled.sock 2>/dev/null || true
        chmod 0660 /var/run/tailscale/tailscaled.sock 2>/dev/null || true
      done
    ) &

    cat > /usr/local/bin/tailscale <<'TSWRAP'
#!/bin/sh
# Raise the box's own tailscale daemon the first time anything asks for it.
if [ ! -S /var/run/tailscale/tailscaled.sock ]; then
  echo start > /run/broker-tailscale-start 2>/dev/null || true
  waited=0
  while [ ! -S /var/run/tailscale/tailscaled.sock ]; do
    waited=$((waited + 1))
    [ "$waited" -gt 60 ] && break
    sleep 0.25
  done
fi
exec /usr/local/bin/tailscale.real "$@"
TSWRAP
    chmod 0755 /usr/local/bin/tailscale
  fi

  # Ahead of the real docker on PATH (/usr/local/bin comes before /usr/bin).
  # `docker compose` is a plugin of this same command, so it is covered too.
  cat > /usr/local/bin/docker <<'WRAPPER'
#!/bin/sh
# Raise the box's own docker daemon the first time anything asks for it.
if ! /usr/bin/docker info >/dev/null 2>&1; then
  echo start > /run/broker-docker-start 2>/dev/null || true
  waited=0
  while ! /usr/bin/docker info >/dev/null 2>&1; do
    waited=$((waited + 1))
    [ "$waited" -gt 80 ] && break
    sleep 0.5
  done
fi
exec /usr/bin/docker "$@"
WRAPPER
  chmod 0755 /usr/local/bin/docker
fi

# The places a personal machine keeps its own commands. Without them a box has
# a PATH the image chose, and anything mounted from your ~/bin is invisible:
# an agent told to run one of your tools gets "command not found" and goes
# looking for the binary across the whole disk.
PATH="$HOME/bin:$HOME/.local/bin:$PATH"
export PATH

if [ -n "${BROKER_BOX_UID:-}" ] && [ "$(id -u)" = "0" ]; then
  exec setpriv --reuid "$BROKER_BOX_UID" --regid "${BROKER_BOX_GID:-0}" --clear-groups "$@"
fi
exec "$@"
