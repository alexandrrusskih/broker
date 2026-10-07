"""The listener side of the bridge: one connection, one fresh server.

Started as `python -m broker.mcpbridge serve`; the entry point is there, and
the state file that says where this listener answers is here.
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
IDLE_TIMEOUT = int(os.environ.get("BROKER_MCP_IDLE_SECONDS") or 4 * 3600)


def _state_file(name, key=""):
    stem = name.replace("/", "_")
    if key:
        stem += "-" + key
    return os.path.join(STATE_DIR, "%s.json" % stem)


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


def _bind_address():
    """Where a box can reach this listener, and nothing beyond this machine can.

    A box dials host.docker.internal. Docker Desktop forwards that name to the
    host's loopback, so 127.0.0.1 is enough there. Docker Engine on Linux maps
    it to the docker0 gateway instead, and a listener on 127.0.0.1 refuses the
    connection — the lane died with 'Connection refused' on WSL. The gateway
    address is a local interface: the host and its containers reach it, the
    network does not.
    """
    override = os.environ.get("BROKER_BRIDGE_BIND")
    if override:
        return override
    if sys.platform.startswith("linux"):
        try:
            import fcntl
            import struct
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                packed = fcntl.ioctl(probe.fileno(), 0x8915,  # SIOCGIFADDR
                                     struct.pack("256s", b"docker0"))
            return socket.inet_ntoa(packed[20:24])
        except OSError:
            pass
    return "127.0.0.1"


def serve(name, command, key=""):
    """Run the listener for one server, for one caller identity, then block."""
    token = secrets.token_hex(16)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    host = _bind_address()
    listener.bind((host, 0))
    listener.listen(16)
    port = listener.getsockname()[1]

    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    state = {"host": host, "port": port, "token": token, "command": command,
             "pid": os.getpid(), "identity": key}
    path = _state_file(name, key)
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    # A client that is still talking keeps this alive. The timeout exists to
    # collect listeners whose box is gone, and it used to be measured between
    # CONNECTIONS — which says nothing about whether anyone is connected. A
    # harness opens its MCP server once at startup and holds that one
    # connection for as long as the session lasts, so after four hours the
    # accept() timed out, this process returned, and its worker threads —
    # daemons, all of them — died with it. The conversation lost its server
    # mid-sentence, with nothing in the log to say why.
    open_sessions = [0]
    counted = threading.Lock()

    def session(conn):
        try:
            _session(conn, command, token)
        finally:
            with counted:
                open_sessions[0] -= 1

    listener.settimeout(IDLE_TIMEOUT)
    try:
        while True:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                with counted:
                    if open_sessions[0]:
                        continue  # someone is still on the line
                return 0  # nobody connected, nobody came back: the box is gone
            with counted:
                open_sessions[0] += 1
            threading.Thread(target=session, args=(conn,), daemon=True).start()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
