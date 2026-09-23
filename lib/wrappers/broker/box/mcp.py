"""MCP servers a box would otherwise lose.

Half of them cannot come along: they are native macOS binaries, and a Linux
container has nothing to run them with. So the server stays on the host and only
its stdio is carried across — a listener here, a stand-in mounted at the
server's own command path there.
"""

import json
import os
import subprocess
import sys
import time

from .. import config
from ..out import warn
from .paths import expand, home_dir

# Inside a container "localhost" is the container. This is the host.
HOST_GATEWAY = "host.docker.internal"

def mcp_servers(provider, env=None):
    """The MCP servers this harness declares that a box would otherwise lose.

    Only the ones started as a COMMAND: a server reached over http needs nothing
    from us, it is already reachable from inside the box.

    Read from the profile this run actually uses, not from the canonical home.
    An account's profile carries its own copy of the config, and the two drift:
    here one spelled a server's path through ~/Projects and the other through
    /Volumes, so the box mounted the stand-in where the harness never looked and
    reported the server missing.
    """
    spec = getattr(provider, "MCP_CONFIG", None)
    if not spec:
        return {}
    path, kind, key = spec
    path = expand(path)

    home_env = getattr(provider, "HOME_ENV", None)
    canonical = getattr(provider, "CANONICAL_HOME", None)
    in_use = (env or {}).get(home_env) if home_env else None
    if in_use and canonical:
        in_use = os.path.abspath(expand(in_use))
        canonical = os.path.abspath(expand(canonical))
        if path.startswith(canonical + os.sep):
            path = os.path.join(in_use, os.path.relpath(path, canonical))
    try:
        if kind == "toml":
            import tomllib

            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        elif kind == "jsonc":
            # JSON with comments. Written by hand, so it has them.
            from .boxes import _strip_comments

            with open(path) as fh:
                data = json.loads(_strip_comments(fh.read()))
        else:
            with open(path) as fh:
                data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        warn("could not read %s for MCP servers: %s" % (path, exc))
        return {}
    found = {}
    for name, server in (data.get(key) or {}).items():
        command = server.get("command")
        if not command:
            continue  # http/sse: the box reaches it over the network
        # Some spell the command as a string plus arguments, others as one
        # list. Both mean the same thing.
        if isinstance(command, (list, tuple)):
            argv = [str(part) for part in command]
        else:
            argv = [command] + list(server.get("args") or [])
        found[name] = {
            "command": argv,
            # "environment" is what opencode calls it; "env" everywhere else.
            "env": dict(server.get("env") or server.get("environment") or {}),
            # codex declares which variables a server expects to inherit rather
            # than spelling out their values. Inside a box nothing is inherited,
            # so the server starts without them — and one that needs them to
            # know who it is answering for simply exposes no tools, which reads
            # as "server missing" and is nothing of the kind.
            "inherit": [v for v in (server.get("env_vars") or []) if isinstance(v, str)],
        }
    return found


def _bridge_env(name, server, profile, projects):
    """The environment the host-side server runs with, for THIS box.

    A server that answers questions about code must answer about the code this
    box is for. So what the box says wins, and where it says nothing the box's
    own project is the default — a root still pointing at the whole machine
    would let one box ask about code it cannot even see.
    """
    env = dict(os.environ)
    env.update(server.get("env") or {})
    override = ((profile.get("mcp") or {}).get(name) or {}).get("env") or {}
    for key, value in override.items():
        value = expand(str(value))
        # A value that IS a path becomes the physical one. Writing
        # ~/Projects/foo where that is a symlink would otherwise key a
        # code-memory database under a second name and reindex from scratch —
        # a trap you would have to remember every time you edited the file.
        if value.startswith("/") and os.path.exists(value):
            value = os.path.realpath(value)
        env[key] = value
    # The physical path, not the one you typed: this server keys its per-project
    # database off the path it is given, so the symlinked spelling would start a
    # second database and reindex the whole project from scratch.
    if projects and "CBM_ALLOWED_ROOT" in env and "CBM_ALLOWED_ROOT" not in override:
        env["CBM_ALLOWED_ROOT"] = os.path.realpath(projects[0])
    return env


def _start_bridge(name, server, profile, projects):
    """Make sure a listener for this server is up, and say how to reach it."""
    from .. import mcpbridge

    # Same server, same caller identity — anything else gets its own listener.
    # Which listener this is. Same server and same caller is the same listener
    # — but ALSO the same environment: a server started once keeps whatever it
    # was started with, and a variable the person sets on the command line to
    # change how it behaves would be ignored by a listener raised before they
    # typed it. That is not a small thing when the variable decides WHO the
    # server acts as: the work is done under the wrong name, and the store
    # refuses it as read-only, which reads as a permissions problem rather than
    # as a stale process.
    #
    # So the values of the variables this server actually receives from the
    # environment are part of its identity. Change one, get your own listener.
    matters = list(((profile.get("mcp") or {}).get(name) or {}).get("identity_env") or ())
    matters += [v for v in (server.get("inherit") or []) if v not in matters]
    matters += [v for v in (server.get("env") or {}) if v not in matters]
    key = mcpbridge.identity_key(extra=tuple(matters))
    live = mcpbridge.running(name, key)
    if live and live.get("command") == server["command"]:
        return live
    try:
        subprocess.Popen(
            [sys.executable, "-m", "broker.mcpbridge", "serve", name, "--"] + server["command"],
            # The directory the package is imported FROM: this file sits in
            # broker/box/, so that is three levels up. It was two before the
            # engine was split into modules, and the bridge silently stopped
            # starting — "the bridge did not come up", with every MCP server
            # missing inside the box.
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            env=_bridge_env(name, server, profile, projects),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # it outlives this process: the box is its client
        )
    except OSError as exc:
        warn("could not bridge the %s MCP server: %s" % (name, exc))
        return None
    for _ in range(50):
        live = mcpbridge.running(name, key)
        if live:
            return live
        time.sleep(0.1)
    warn("the %s MCP bridge did not come up — it will be missing inside the box" % name)
    return None


# The shim carries its own client, so the image has to know nothing about any of
# this — it only needs python3, which a harness image has anyway.
SHIM_TEMPLATE = "\n".join([
    "#!/usr/bin/env python3",
    "# Written by the broker: %(name)s runs on the host; this is the wire to it.",
    "import socket, sys, threading",
    "HOST, PORT, TOKEN = %(host)r, %(port)d, %(token)r",
    "",
    "",
    "def pump(src, dst, done=None):",
    "    try:",
    "        while True:",
    "            if hasattr(src, 'recv'):",
    "                chunk = src.recv(65536)",
    "            else:",
    "                # read1: read() would block for a full buffer and deadlock",
    "                chunk = src.read1(65536) if hasattr(src, 'read1') else src.read(65536)",
    "            if not chunk:",
    "                break",
    "            if hasattr(dst, 'sendall'):",
    "                dst.sendall(chunk)",
    "            else:",
    "                dst.write(chunk)",
    "                dst.flush()",
    "    except (OSError, ValueError):",
    "        pass",
    "    finally:",
    "        if done:",
    "            try:",
    "                done()",
    "            except OSError:",
    "                pass",
    "",
    "",
    "# IPv4 explicitly. The host's name resolves to both families in here, the",
    "# v6 address has no route, and it is tried first — so a bridge that is",
    "# simply not running reported 'Network is unreachable' from the v6 attempt",
    "# instead of 'Connection refused' from the one that matters, and sent",
    "# whoever read it looking at the network.",
    "addresses = socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)",
    "family, kind, proto, _, address = addresses[0]",
    "with socket.socket(family, kind, proto) as conn:",
    "    conn.connect(address)",
    "    conn.sendall(TOKEN.encode() + b'\\n')",
    "    out = threading.Thread(target=pump, args=(conn, sys.stdout.buffer), daemon=True)",
    "    out.start()",
    "    pump(sys.stdin.buffer, conn, lambda: conn.shutdown(socket.SHUT_WR))",
    "    out.join(timeout=5)",
    "",
])


def _write_shim(provider, name, live):
    """A stand-in for the server's command, to be mounted at its own path.

    One file per window, not one per server. A bind mount holds the INODE it
    was given, and writing this file replaces the inode — so a box starting up
    used to pull the shim out from under every box already running the same
    harness. Inside those, the mount went stale: `ls` showed the entry as
    `-?????????`, the harness could no longer start its server, and the only
    clue was that it had been fine until somebody opened another box. Which is
    exactly what it looks like when a bridge "randomly" drops.
    """
    from .paths import window_key

    directory = os.path.join(config.CONFIG_DIR, "box", "shims", provider.NAME)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "%s-%s" % (name.replace("/", "_"), window_key()))
    body = SHIM_TEMPLATE % {"name": name, "host": HOST_GATEWAY,
                            "port": live["port"], "token": live["token"]}
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.chmod(tmp, 0o700)  # it carries the connection secret
    os.replace(tmp, path)
    return path


