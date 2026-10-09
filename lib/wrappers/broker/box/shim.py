"""The stand-in a box runs in place of an MCP server that stayed on the host."""

import os
import stat

from .. import config
from .mcp import HOST_GATEWAY
from .paths import window_key


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
    return _write(os.path.join(directory, "%s-%s" % (name.replace("/", "_"), window_key())),
                  name, live)


def _write(path, name, live):
    """The connector itself, written atomically and 0700."""
    body = SHIM_TEMPLATE % {"name": name, "host": HOST_GATEWAY,
                            "port": live["port"], "token": live["token"]}
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.chmod(tmp, 0o700)  # it carries the connection secret
    os.replace(tmp, path)
    return path


def connect_mcp(name, command, out, env=None, roots=(), inherit=()):
    """A connector for a host MCP server, for a container that is not a box.

    Written for Crew, which builds its own container and needs the same bridge:
    the server stays on this machine and only its stdio crosses. The listener's
    port, its secret and the text of the connector stay in here; the caller
    gets a file that speaks MCP on stdin and stdout.

      name     the server as the host's own config calls it
      command  its argv on the host, a list
      out      where to write the connector
      env      what the server needs in its environment
      roots    the paths it may be asked about; the first is the one a
               root-scoped server is pinned to, physical path, as for a box
      inherit  names of host variables the listener carries into the server

    A live listener is reused only when its command AND its caller identity
    match — the identity folds in the pane, the workspace, the bus home, every
    variable named in `env`, and the effective allowed root. Two callers that
    differ in any of those get their own listener, which is the whole point:
    a server answering about code must answer about the caller's code.

    Returns the path written. Raises LookupError when no listener could be
    started.

    One path per concurrent container. A bind mount holds the inode it was
    given and this replaces the file, so sharing one path between two live
    containers pulls the connector out from under the first — which looks
    exactly like a bridge dropping at random.
    """
    from .mcp import _start_bridge

    server = {"command": list(command), "env": dict(env or {}), "inherit": list(inherit)}
    # identity_env says which values decide whether a listener may be reused.
    # The names from `env` are added by _start_bridge itself; the allowed root
    # is named here because it can arrive through `roots` instead.
    profile = {"env": dict(env or {}), "mcp": {name: {"identity_env": ["CBM_ALLOWED_ROOT"]}}}
    live = _start_bridge(name, server, profile, [str(r) for r in roots])
    if not live:
        raise LookupError("no listener for the %r MCP server could be started" % name)
    return _write(out, name, live)
