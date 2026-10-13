"""The stand-in a box runs in place of an MCP server that stayed on the host."""

import json
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
    "HOST, PORT, TOKEN = %(host)s, %(port)d, %(token)s",
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


# The same wire for a container that has node and no python. Written for Crew,
# which is removing python from its own image: a connector the box itself can
# run is no use to them if it needs an interpreter they no longer ship. Same
# protocol, same secret on the first line, so the listener cannot tell the two
# apart — which is the point. Streams rather than a hand-written pump: node
# does the backpressure the python version has to do by hand with read1.
SHIM_TEMPLATE_JS = "\n".join([
    "#!/usr/bin/env node",
    "// Written by the broker: %(name)s runs on the host; this is the wire to it.",
    '"use strict";',
    'const net = require("net");',
    "const HOST = %(host)s, PORT = %(port)d, TOKEN = %(token)s;",
    "",
    "// IPv4 explicitly, for the reason the python connector gives: the host's",
    "// name resolves to both families in here, the v6 address has no route and",
    "// is tried first, so a bridge that is simply not running reported an",
    "// unreachable network instead of the refused connection that matters.",
    "const conn = net.connect({host: HOST, port: PORT, family: 4});",
    "",
    'conn.on("connect", () => {',
    '  conn.write(TOKEN + "\\n");',
    "  // stdin's end half-closes the socket, which is the SHUT_WR the server",
    "  // waits for; stdout is not ours to close, so the other way does not end.",
    "  process.stdin.pipe(conn);",
    "  conn.pipe(process.stdout, {end: false});",
    "});",
    "",
    "// Never process.exit here: it would drop whatever stdout has still to",
    "// flush. Releasing stdin lets the process end once the pipes are empty.",
    'conn.on("close", () => process.stdin.destroy());',
    'conn.on("error", (error) => {',
    "  process.exitCode = 1;",
    '  process.stderr.write("broker bridge: " + error.message + "\\n");',
    "  process.stdin.destroy();",
    "  conn.destroy();",
    "});",
    "",
])

SHIM_TEMPLATES = {"py": SHIM_TEMPLATE, "js": SHIM_TEMPLATE_JS}


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


def _write(path, name, live, lang="py"):
    """The connector itself, written atomically and 0700."""
    try:
        template = SHIM_TEMPLATES[lang]
    except KeyError:
        raise ValueError("unknown connector language %r — use 'py' or 'js'" % lang)
    # Quoting belongs to the language the file is in, so the template asks for
    # an already-quoted value. The name only reaches a comment, but a newline
    # in it would end that comment and leave the rest of the name as code —
    # and since the CLI took a name as an argument, that name is now input.
    quote = repr if lang == "py" else json.dumps
    body = template % {"name": name.replace("\n", " ").replace("\r", " "),
                       "host": quote(HOST_GATEWAY),
                       "port": live["port"], "token": quote(live["token"])}
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.chmod(tmp, 0o700)  # it carries the connection secret
    os.replace(tmp, path)
    return path


def connect_mcp(name, command, out, env=None, roots=(), inherit=(), lang="py",
                identity=()):
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
      lang     the language the connector is written in, "py" or "js". The
               wire is the same either way; pick the interpreter the
               container actually has.
      identity names of variables that decide whether a live listener may be
               reused, beyond those `env` already names. Pass the one that
               carries the allowed root when it arrives through `roots`:
               nothing else would tell two callers with different roots apart,
               and they would share one listener answering about one of them.

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
    # identity_env says which values decide whether a listener may be reused,
    # and identity_key hashes the VALUES of the names it is given. So a root
    # that arrives through `roots` is in no environment yet and would not be
    # part of the identity at all: two callers differing only in their root
    # would share one listener, and it would answer about one of them.
    #
    # This used to be solved by naming ["CBM_ALLOWED_ROOT"] here — one MCP
    # server's variable, in the call every other server also goes through. It
    # held only while that server was the only caller. The root goes into the
    # environment under a name of OURS instead, which _start_bridge then folds
    # in like any other, so the property holds for every server and the caller
    # has no flag to remember. `identity` stays for anything else it wants
    # counted.
    bridge = dict(env or {})
    if roots:
        bridge.setdefault("BROKER_MCP_ROOT", os.path.realpath(str(roots[0])))
    profile = {"env": bridge,
               "mcp": {name: {"identity_env": [str(v) for v in identity]}}}
    live = _start_bridge(name, server, profile, [str(r) for r in roots])
    if not live:
        raise LookupError("no listener for the %r MCP server could be started" % name)
    return _write(out, name, live, lang)
