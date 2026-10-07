"""Reaching the machine's own MCP servers from inside a box.

Half of them cannot come along: they are native macOS binaries, and a Linux
container has nothing to run them with. Copying them into the image
is not an option either — they are private tools with their own dependencies,
and an image that bundles them goes stale the moment they change.

So the server stays on the host and only its stdio is carried across. On the
host a listener accepts one connection per client and hands it a freshly spawned
server; inside the box a tiny client connects and becomes a pipe. The harness
sees exactly what it saw before: a command that speaks MCP on stdin and stdout.

Why TCP and not a unix socket: Docker Desktop shares files through a filesystem
layer that does not carry socket semantics, so a bind-mounted socket cannot be
connected to from a container. A loopback port can — through
host.docker.internal — but a loopback port is reachable by every process on the
machine, so the first line a client sends is a secret handed to it in the
environment, and a connection that does not match is closed before a server is
spawned.
"""

import hashlib
import json
import os
import socket
import sys
import threading

from .mcpserve import _pump, _state_file, serve

# A bridged server inherits the environment of whatever raised it, and then
# outlives that shell. agentbus is the clear case: it takes its bus identity
# from the Herdr pane it was started in, so a listener raised from one pane and
# reused from another would post to the bus as the wrong agent, in the wrong
# workspace. So a listener belongs to the identity that raised it, and a
# different identity gets its own.
IDENTITY_ENV = ("HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "AGENTBUS_HOME")


def identity_key(env=None, extra=()):
    env = os.environ if env is None else env
    seen = [(name, env.get(name) or "") for name in tuple(IDENTITY_ENV) + tuple(extra)]
    if not any(value for _, value in seen):
        return ""
    digest = hashlib.sha1(repr(sorted(seen)).encode()).hexdigest()[:8]
    return digest


def running(name, key=""):
    """The live listener for this server AND this caller identity, or None."""
    try:
        with open(_state_file(name, key)) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    try:
        os.kill(state["pid"], 0)
    except (OSError, KeyError, TypeError):
        return None
    return state


def connect(host, port, token):
    """The client end, run INSIDE the box: become a pipe to the host's server."""
    with socket.create_connection((host, int(port))) as conn:
        conn.sendall(token.encode() + b"\n")
        out = threading.Thread(
            target=_pump, args=(conn, sys.stdout.buffer), daemon=True)
        out.start()
        _pump(sys.stdin.buffer, conn, lambda: conn.shutdown(socket.SHUT_WR))
        out.join(timeout=5)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        name = argv[1]
        command = argv[argv.index("--") + 1:]
        # The caller works out which listener this is and SAYS so, rather than
        # letting this process work it out again from its own environment. It
        # cannot: what the listener is started with is not what the caller was
        # holding — the bridge is given the server's own variables on top — so
        # the two arrived at different answers, the listener registered under
        # one name and the caller waited five seconds for the other. Every
        # bridged server then reported itself missing inside the box, while the
        # http ones, which need no bridge, connected fine.
        key = argv[argv.index("--key") + 1] if "--key" in argv[:argv.index("--")] else identity_key()
        return serve(name, command, key)
    if argv and argv[0] == "connect":
        # Inside the box everything comes from the environment the shim sets.
        return connect(os.environ["BROKER_MCP_HOST"],
                       os.environ["BROKER_MCP_PORT"],
                       os.environ["BROKER_MCP_TOKEN"])
    sys.stderr.write("usage: mcpbridge serve <name> -- <command...> | mcpbridge connect\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
