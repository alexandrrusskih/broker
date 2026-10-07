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
    path = os.path.join(directory, "%s-%s" % (name.replace("/", "_"), window_key()))
    body = SHIM_TEMPLATE % {"name": name, "host": HOST_GATEWAY,
                            "port": live["port"], "token": live["token"]}
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.chmod(tmp, 0o700)  # it carries the connection secret
    os.replace(tmp, path)
    return path
