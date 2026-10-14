"""The listener side of the bridge: one connection, one fresh server.

Started as `python -m broker.mcpbridge serve`; the entry point is there, and
the state file that says where this listener answers is here.
"""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading

from . import config


def state_dir():
    """Where a listener records itself, asked EVERY time.

    Not frozen at import, for the reason box/report.py gives at its own root():
    config.CONFIG_DIR is moved at run time, and a value taken once writes into
    the real home whatever the caller said. Third instance of the same mistake
    in this package, found by codex-misc-p5 in the first.
    """
    return os.path.join(config.CONFIG_DIR, "box", "mcp")
# A listener with nothing connected for this long has outlived the box that
# asked for it. Without this every box ever started would leave a process behind.
IDLE_TIMEOUT = int(os.environ.get("BROKER_MCP_IDLE_SECONDS") or 4 * 3600)


def _state_file(name, key=""):
    stem = name.replace("/", "_")
    if key:
        stem += "-" + key
    return os.path.join(state_dir(), "%s.json" % stem)


class _Sender:
    """The client end of one session, written to by two threads.

    The downstream pump carries the server's answers, and a refused request is
    answered from the filtering thread. Two sendall() calls at once interleave
    their bytes, which would split a JSON line down the middle and leave the
    harness parsing nonsense — so every write to the client goes through here.
    """

    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.Lock()

    def sendall(self, data):
        with self._lock:
            self._conn.sendall(data)

    def send_line(self, message):
        self.sendall(json.dumps(message).encode() + b"\n")


# What a box may ask of a bridged server. Absent — and that is the default —
# nothing is filtered and the bytes move as they always did.
#
# Why here and not in the server: the server runs on the HOST, outside the box,
# and opens the host's files itself. A box's mounts do not constrain it, and
# codebase-memory, the case this was written for, keys its allowed root to
# INDEXING only: a box with a root of its own could still read every project in
# the shared base. The listener is the one place that sees each request and
# belongs to exactly one box, so it is the only place a per-box rule can hold.
#
#   {"deny_tools": ["delete_project"],
#    "args": {"project": ["Volumes-hdd-Projects-joppa", "joppa"]}}
#
# Each pattern must match the WHOLE value. An argument the policy does not name
# is not checked, and a request that is not a tools/call passes untouched.
POLICY_ENV = "BROKER_MCP_POLICY"

# A request is a few hundred bytes; a line this long is not one. Refusing is the
# only honest answer: forwarding what could not be parsed would be a way around
# the rule, and dropping it silently would look like a server that hung.
MAX_LINE = 16 * 1024 * 1024


def _policy():
    """The rule this listener was started with, taken out of the environment.

    Removed from os.environ once read, so the MCP server this listener spawns
    does not inherit a variable that is none of its business.
    """
    raw = os.environ.pop(POLICY_ENV, "")
    if not raw:
        return None
    try:
        policy = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(policy, dict) or not (policy.get("args") or policy.get("deny_tools")):
        return None
    compiled = {}
    for argument, patterns in (policy.get("args") or {}).items():
        compiled[str(argument)] = [re.compile(str(p)) for p in patterns]
    return {"args": compiled,
            "deny_tools": [str(t) for t in (policy.get("deny_tools") or [])]}


def _refuse(message, policy):
    """Why this request may not go through, or None if it may."""
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    tool = params.get("name")
    if tool in policy["deny_tools"]:
        return "the %r tool is not available to this box" % tool
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return None
    for argument, patterns in policy["args"].items():
        if argument not in arguments:
            continue
        values = arguments[argument]
        values = values if isinstance(values, (list, tuple)) else [values]
        for value in values:
            if not isinstance(value, str) or not any(p.fullmatch(value) for p in patterns):
                return ("%s=%r is outside what this box may ask %s about"
                        % (argument, value, tool))
    return None


def _filter(src, dst, policy, client, close_on_done=None):
    """The upstream pump, one JSON line at a time, refusing what the rule denies.

    MCP over stdio delimits messages with newlines and allows none inside one,
    so a line is a message. A line that is not JSON goes through as it is: the
    server's own parser gives a better complaint than a guess would.
    """
    buffer = b""
    try:
        while True:
            chunk = src.recv(65536) if hasattr(src, "recv") else src.read1(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                stripped = line.strip()
                if not stripped:
                    dst.write(line + b"\n")
                    dst.flush()
                    continue
                try:
                    message = json.loads(stripped)
                except ValueError:
                    message = None
                refused = None
                if message is not None:
                    for one in (message if isinstance(message, list) else [message]):
                        refused = _refuse(one, policy)
                        if refused:
                            break
                if refused:
                    identifier = None
                    for one in (message if isinstance(message, list) else [message]):
                        if isinstance(one, dict) and one.get("id") is not None:
                            identifier = one["id"]
                            break
                    if identifier is not None:
                        client.send_line({"jsonrpc": "2.0", "id": identifier,
                                          "error": {"code": -32600,
                                                    "message": "broker: " + refused}})
                    continue
                dst.write(line + b"\n")
                dst.flush()
            if len(buffer) > MAX_LINE:
                client.send_line({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32600,
                                            "message": "broker: one request exceeded %d bytes"
                                                       % MAX_LINE}})
                break
    except (OSError, ValueError):
        pass
    finally:
        if close_on_done is not None:
            try:
                close_on_done()
            except OSError:
                pass


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


def _session(conn, command, token, policy=None):
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
        client = _Sender(conn)
        try:
            # Only the box-to-server direction is read line by line: a rule
            # applies to requests, and the answers can stay raw bytes, which
            # keeps a large snippet moving as it always did.
            carry = _filter if policy else _pump
            arguments = ((conn, child.stdin, policy, client, child.stdin.close) if policy
                         else (conn, child.stdin, child.stdin.close))
            up = threading.Thread(target=carry, args=arguments, daemon=True)
            up.start()
            _pump(child.stdout, client)
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
    policy = _policy()
    token = secrets.token_hex(16)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    host = _bind_address()
    listener.bind((host, 0))
    listener.listen(16)
    port = listener.getsockname()[1]

    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    state = {"host": host, "port": port, "token": token, "command": command,
             "pid": os.getpid(), "identity": key, "filtered": bool(policy)}
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
            _session(conn, command, token, policy)
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
