"""The listener side of the bridge: one connection, one fresh server.

Started as `python -m broker.mcpbridge serve`; the entry point is there, and
the state file that says where this listener answers is here.
"""

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time

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


def started(pid):
    """When this process began, as the system reports it, or "" if unknown.

    Only needs to be comparable with itself: two readings of one live process
    match, and a pid that has been handed to somebody else since does not.
    """
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _state_file(name, key=""):
    stem = name.replace("/", "_")
    if key:
        stem += "-" + key
    return os.path.join(state_dir(), "%s.json" % stem)


def log_file(name, key=""):
    """Where a bridged server's own words go.

    They used to go nowhere. box/mcp.py started the listener with
    stderr=DEVNULL, and a server spawned by a session inherits that, so
    everything a server said about itself was discarded. Measured on one
    machine: mcp-remote printed "Please authorize this client by visiting:
    <url>" fourteen hundred times over ten hours, and not one of those lines
    existed anywhere afterwards. The sign-in was the only way out of the
    failure and nothing on the machine would say where to go.

    A listener's own traceback went the same way, which is why "the bridge did
    not come up" has never had a reason attached to it.

    Private, like the state file beside it: a server that fails while holding a
    credential may well name it on the way down.
    """
    stem = name.replace("/", "_")
    if key:
        stem += "-" + key
    return os.path.join(state_dir(), "%s.log" % stem)


# One generation of a log is kept, and this is where the live one is rolled.
LOG_MAX = 1 << 20


def open_log(name, key=""):
    """The log to hand a listener as its stderr, rolled if the old one is big.

    Rolled by whoever STARTS the listener, because that is the only moment
    anybody owns the file's lifetime: the listener then holds the descriptor
    for as long as it lives, and a server appending through it cannot be made
    to change files underneath. So the bound is per listener rather than
    absolute. A server that talks for a whole day in one session outgrows it,
    and that is the trade for never losing the FIRST lines, which are the ones
    that say why.
    """
    path = log_file(name, key)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass  # not there yet, or not ours to move: appending is still right
    handle = open(path, "a")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return handle


# A listener spawns a fresh server for every connection, and a harness
# reconnects on a schedule of its own. So a server that is BROKEN rather than
# busy is started again, and again, for as long as it stays broken, and each
# start fails the same way. Measured on one machine: fourteen hundred doomed
# starts of a single command in ten hours, with nothing counting them.
#
# A command that dies at once WITHOUT EVER SPEAKING MCP, enough times in a row,
# is therefore left alone for a while, and the client is told why instead of
# being handed another corpse. See _breaker_note for what does not count.
FAST_FAIL_SECONDS = 5
FAST_FAIL_LIMIT = 5
COOLDOWN_SECONDS = 60
COOLDOWN_MAX = 15 * 60
# The count is shared by every listener for this command, so one number stands
# for the whole machine. Keyed by the command and not by the server's name,
# because the same command under two names is one broken thing, and two
# commands under one name are two.
BREAKER_ENV = "BROKER_MCP_BREAKER"


def _breaker_off():
    return os.environ.get(BREAKER_ENV) == "0"


def _breaker_file(command):
    digest = hashlib.sha1(repr(list(command)).encode()).hexdigest()[:12]
    return os.path.join(state_dir(), "fail-%s.json" % digest)


def _breaker(command):
    try:
        with open(_breaker_file(command)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _breaker_write(command, state):
    path = _breaker_file(command)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.new" % (path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _breaker_clear(command):
    try:
        os.remove(_breaker_file(command))
    except OSError:
        pass


def _cooling(command, about=None):
    """The reason to refuse this spawn, or "" to go ahead.

    Two listeners can read a stale count and both spawn. That is the right way
    round for a guard like this: it exists to stop a run of hundreds, and a
    doubled start costs one process.
    """
    if _breaker_off():
        return ""
    state = _breaker(command)
    if not state:
        return ""
    left = (state.get("until") or 0) - time.time()
    if left <= 0:
        return ""
    return ("%s stopped at once %d times in a row, so it is not being started "
            "again for %ds. What it said is in %s"
            % ((about or {}).get("name") or command[0], state.get("fails") or 0,
               int(left + 0.5), (about or {}).get("log") or "the bridge log"))


def _breaker_note(command, lifetime, own_death, spoke):
    """Record how that start went, so the next one can decide.

    Three things have to hold before a start counts as a failure, and each one
    was a false positive without it.

    `own_death` separates a server that fell over from one this listener ended
    because its client went away. Without it every ordinary disconnect read as
    a failure.

    `spoke` is whether the server wrote any MCP at all. A stdio server exits
    when its stdin closes, so a short, perfectly good session — connect, ask,
    close — ends with the server exiting by itself in under a second. Five of
    those would have held off a server that was answering every time. One that
    never spoke is the case this guard is for.

    `lifetime` keeps a server that ran for a while and then died out of it:
    that is a server with a problem of its own, and refusing to start it again
    hides the problem instead of showing it.
    """
    if _breaker_off():
        return
    if not own_death or spoke or lifetime >= FAST_FAIL_SECONDS:
        # A good start clears the whole history: a server that was down and
        # came back must not be held off for something it no longer does.
        if _breaker(command):
            _breaker_clear(command)
        return
    fails = ((_breaker(command) or {}).get("fails") or 0) + 1
    wait = 0
    if fails >= FAST_FAIL_LIMIT:
        # Doubling, so a thing that stays broken is asked about less and less,
        # and capped, so it is still asked about: the cure is usually a person
        # doing something, and the next start is what tells them it worked.
        wait = min(COOLDOWN_MAX,
                   COOLDOWN_SECONDS * (2 ** min(fails - FAST_FAIL_LIMIT, 10)))
    _breaker_write(command, {"fails": fails, "until": time.time() + wait})


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
    """Move bytes as they arrive, and say how many there were.

    read1() rather than read(): on a pipe, read(n) blocks until it has all n
    bytes, so a request smaller than the buffer would sit there unanswered until
    the next one arrived — a bridge that deadlocks on every first message.

    The count is for the one caller that needs it: a server which never wrote a
    byte of MCP did not start, whatever its exit code said. See _breaker_note.
    """
    moved = 0
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
            moved += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        if close_on_done is not None:
            try:
                close_on_done()
            except OSError:
                pass
    return moved


class _Live:
    """The children of the sessions still running, so an exit can take them too.

    _session cleans up its own child in a finally, and while the listener lives
    that is the only path that matters. But those finallys run in DAEMON
    threads: when this process returns, the interpreter kills them where they
    stand and no finally runs at all. The children are re-parented to launchd
    and stay, each holding whatever its server holds — for a code index, most
    of a gigabyte. Measured on one machine: 92 index servers, 30 of them with
    ppid 1 and no bridge left to own them, the oldest 28 minutes old.

    So the listener keeps the set as well, and empties it on the way out. The
    thread's own finally still runs first on every ordinary disconnect; this
    only catches what is left when there is no thread any more.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._children = set()

    def add(self, child):
        with self._lock:
            self._children.add(child)

    def drop(self, child):
        with self._lock:
            self._children.discard(child)

    def close(self):
        """Ask them all to go, then make sure they went."""
        with self._lock:
            children = list(self._children)
            self._children = set()
        # Terminated in one pass before any wait, so a slow one does not hold
        # the next five seconds of the shutdown for the rest of them.
        for child in children:
            try:
                child.terminate()
            except OSError:
                pass
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except OSError:
                    pass
            except OSError:
                pass


def _session(conn, command, token, policy=None, live=None, about=None):
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

        cooling = _cooling(command, about)
        if cooling:
            conn.sendall(json.dumps({"error": cooling}).encode() + b"\n")
            return
        began = time.monotonic()
        try:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError as exc:
            conn.sendall(json.dumps({"error": str(exc)}).encode() + b"\n")
            return
        if live is not None:
            live.add(child)
        client = _Sender(conn)
        # Set before the try, because the finally below reads it: an exception
        # on the way to the pump would otherwise raise a NameError over the
        # real failure and lose it.
        spoke = False
        try:
            # Only the box-to-server direction is read line by line: a rule
            # applies to requests, and the answers can stay raw bytes, which
            # keeps a large snippet moving as it always did.
            carry = _filter if policy else _pump
            arguments = ((conn, child.stdin, policy, client, child.stdin.close) if policy
                         else (conn, child.stdin, child.stdin.close))
            up = threading.Thread(target=carry, args=arguments, daemon=True)
            up.start()
            spoke = bool(_pump(child.stdout, client))
        finally:
            # Shut the socket down before `with conn` closes it, or the client
            # is never told the server is gone.
            #
            # The upstream thread sits in conn.recv() for as long as the box has
            # the connection open. When the server exits, the downstream pump
            # returns, this block runs, and close() happens with that recv()
            # still pending — and on macOS a close() in one thread neither
            # wakes a recv() blocked in another nor sends the FIN. So the shim
            # inside the box waited for an EOF that was never coming: the
            # harness showed a server that had already died as merely silent,
            # for as long as the box lived. Found by the equill setup on an
            # `ssh -T` that dropped mid-session, where the server is remote and
            # dying is ordinary.
            #
            # shutdown() does both jobs: it wakes the blocked reader and it
            # sends the FIN the shim is waiting for.
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already gone the other way; close() is enough
            # Read BEFORE we end it. Afterwards every exit looks like the one
            # this line is about to cause, and a healthy server would be
            # counted as a failure on every ordinary disconnect.
            _breaker_note(command, time.monotonic() - began,
                          child.poll() is not None, spoke)
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
            if live is not None:
                live.drop(child)


def bind_address():
    """Where a box can reach this listener, and nothing beyond this machine can.

    Public, because a connector written for a HOST caller has to dial exactly
    what this binds: host.docker.internal is a name only a container resolves.
    See box/shim.py, target="host".

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
    host = bind_address()
    listener.bind((host, 0))
    listener.listen(16)
    port = listener.getsockname()[1]

    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    state = {"host": host, "port": port, "token": token, "command": command,
             "pid": os.getpid(), "identity": key, "filtered": bool(policy),
             # The pid alone does not identify a process: pids are reused, and
             # a state file outlives the listener it describes. Its start time
             # does, so a reader can tell "still running" from "somebody else
             # now holds that number". See mcpbridge.running.
             "started": started(os.getpid())}
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
    live = _Live()
    # What to call this server, and where its own words are, for the one
    # message a refused client gets instead of a server.
    about = {"name": name, "log": log_file(name, key)}

    def session(conn):
        try:
            _session(conn, command, token, policy, live, about)
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
        # Before the state file goes: a reader that sees no file and a live
        # server would have nothing to connect to and no way to say why.
        live.close()
        try:
            os.remove(path)
        except OSError:
            pass
