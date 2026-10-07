"""A box that runs containers of its own, and the cache they pull through."""

import os
import re
import subprocess

from .. import mcpbridge
from ..out import warn
from . import mcp


# A pull-through cache for the images a box's own docker daemon fetches, run
# once on this machine and shared by every box on it.
#
# Each window's daemon owns its own layer store — two daemons cannot share one
# /var/lib/docker — so every new window used to fetch Postgres and Redis from
# the internet again, and a person who opens and kills boxes all day pays that
# download every time. The cache turns the second fetch and every one after it
# into a copy over the loopback.
#
# Bound to 127.0.0.1: it holds public images and answers only this machine.
REGISTRY_CACHE = "broker-box-registry"
REGISTRY_PORT = 5009
REGISTRY_MIRROR = "http://%s:%d" % (mcp.HOST_GATEWAY, REGISTRY_PORT)


def _ensure_registry_cache(binary):
    """Have the cache running, or carry on without it.

    Never fatal: a box whose images come straight from the internet works
    exactly as it did before, only slower on a cold window.
    """
    try:
        state = subprocess.run([binary, "inspect", "-f", "{{.State.Running}}", REGISTRY_CACHE],
                               capture_output=True, text=True, timeout=20)
        if state.returncode == 0:
            if state.stdout.strip() == "true":
                return True
            return subprocess.run([binary, "start", REGISTRY_CACHE],
                                  capture_output=True, timeout=30).returncode == 0
        started = subprocess.run(
            [binary, "run", "-d", "--name", REGISTRY_CACHE, "--restart", "unless-stopped",
             "-p", "127.0.0.1:%d:5000" % REGISTRY_PORT,
             "-v", "%s-cache:/var/lib/registry" % REGISTRY_CACHE,
             "-e", "REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io",
             "registry:2"], capture_output=True, text=True, timeout=180)
        if started.returncode:
            # One line of prose, not a list: `% a[-1:] or ""` binds as
            # `("…%s" % a[-1:]) or ""`, so this printed the repr of a one-item
            # list — brackets, quotes and all — around the daemon's own words.
            last = (started.stderr or "").strip().splitlines()
            warn("the image cache did not start, images will come from the internet: %s"
                 % (last[-1].strip() if last else "no reason given"))
        return started.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def flags(name, profile, binary):
    """Who the box starts as, and where its own daemon keeps its images."""
    args = []
    # The harness writes as you, not as root: files it creates in the project
    # stay yours, and nothing needs chown afterwards.
    # A box that runs containers starts as root — the daemon needs it — and the
    # entry point drops to this uid before the harness starts. Everything else
    # never becomes root at all.
    wants_docker = bool(profile.get("docker"))
    if wants_docker:
        args += ["--privileged", "-e", "BROKER_BOX_DOCKER=1",
                "-e", "BROKER_BOX_UID=%d" % os.getuid(),
                "-e", "BROKER_BOX_GID=%d" % os.getgid(),
                # Its own layer store, kept between runs: no two boxes share
                # images, and a test suite does not re-pull Postgres every time.
                #
                # Per WINDOW, not just per box: a docker daemon owns
                # /var/lib/docker exclusively, so a second box of the same name
                # — another pane, another agent — found the store taken and
                # started without a daemon at all. Reopening the same window
                # still reuses its images.
                # Sanitised like the image name: docker accepts only
                # [a-zA-Z0-9][a-zA-Z0-9_.-] in a volume name, and a box named in
                # anything else fails to start at all.
                "--mount", "type=volume,source=broker-box-docker-%s%s,target=/var/lib/docker"
                % (re.sub(r"[^a-zA-Z0-9_.-]", "-", name).lower(),
                   ("-" + mcpbridge.identity_key()) if mcpbridge.identity_key() else "")]
        # Where that daemon looks before the internet. The name resolves through
        # the host-gateway line added below.
        if _ensure_registry_cache(binary):
            args += ["-e", "BROKER_BOX_REGISTRY_MIRROR=%s" % REGISTRY_MIRROR]
        args += ["--add-host", "%s:host-gateway" % mcp.HOST_GATEWAY]
    else:
        args += ["--user", "%d:%d" % (os.getuid(), os.getgid())]
    return args
