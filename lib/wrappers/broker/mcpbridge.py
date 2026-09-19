"""Reaching the machine's own MCP servers from inside a box.

Half of them cannot come along: `ntk` and `codebase-memory` are macOS binaries,
and a Linux container has nothing to run them with. Copying them into the image
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

import json
import os
import secrets
import socket
import subprocess
import sys
import threading

from . import config

STATE_DIR = os.path.join(config.CONFIG_DIR, "box", "mcp")
# A listener with nothing connected for this long has outlived the box that
# asked for it. Without this every box ever started would leave a process behind.
IDLE_TIMEOUT = 4 * 3600


def _state_file(name):
    return os.path.join(STATE_DIR, "%s.json" % name.replace("/", "_"))


def _pump(src, dst, close_on_done=None):
    """Move bytes as they arrive.

    read1() rather than read(): on a pipe, read(n) blocks until it has all n
    bytes, so a request smaller than the buffer would sit there unanswered until
    the next one arrived — a bridge that deadlocks on every first message.
    """
    try:
        while True:
            if hasattr(src, "recv"):
                chunk = src.recv(65536)
            else:
                chunk = src.read1(65536) if hasattr(src, "read1") else src.read(65536)
            if not chunk:
                break
            if hasattr(dst, "sendall"):
                dst.sendall(chunk)
            else:
                dst.write(chunk)
                dst.flush()
    except (OSError, ValueError):
        pass
    finally:
        if close_on_done is not None:
            try:
                close_on_done()
            except OSError:
                pass


def _session(conn, command, token):
    """One client: check the secret, spawn a server, be the wire between them."""
    with conn:
        conn.settimeout(10)
        try:
            greeting = b""
            while not greeting.endswith(b"\n") and len(greeting) < 256:
                chunk = conn.recv(1)
                if not chunk:
                    return
                greeting += chunk
        except OSError:
            return
        if not secrets.compare_digest(greeting.strip().decode("utf-8", "replace"), token):
            return  # not ours: say nothing, spawn nothing
        conn.settimeout(None)

        try:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError as exc:
            conn.sendall(json.dumps({"error": str(exc)}).encode() + b"\n")
            return
        try:
            up = threading.Thread(target=_pump, args=(conn, child.stdin, child.stdin.close), daemon=True)
            up.start()
            _pump(child.stdout, conn)
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


def serve(name, command):
    """Run the listener for one server. Prints its port, then blocks."""
    token = secrets.token_hex(16)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]

    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    state = {"port": port, "token": token, "command": command, "pid": os.getpid()}
    path = _state_file(name)
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    listener.settimeout(IDLE_TIMEOUT)
    try:
        while True:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                return 0  # nobody came back; the box is long gone
            threading.Thread(target=_session, args=(conn, command, token), daemon=True).start()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def running(name):
    """The live listener for this server, or None."""
    try:
        with open(_state_file(name)) as fh:
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
        return serve(name, command)
    if argv and argv[0] == "connect":
        # Inside the box everything comes from the environment the shim sets.
        return connect(os.environ["BROKER_MCP_HOST"],
                       os.environ["BROKER_MCP_PORT"],
                       os.environ["BROKER_MCP_TOKEN"])
    sys.stderr.write("usage: mcpbridge serve <name> -- <command...> | mcpbridge connect\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
