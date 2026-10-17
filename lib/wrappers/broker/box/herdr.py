"""The terminal manager, reachable from inside a box.

The socket is not the obstacle. Measured, against the claim that stood in this
docstring before: Docker Desktop does carry herdr's unix socket into a
container, it arrives as a socket, and a process inside connects to it.

The CLIENT is the obstacle. herdr is a Mach-O arm64 executable, and a Linux
container has nothing to run it with. So a box holding that socket would have
nothing able to speak to it, and the alternative — teaching a stand-in herdr's
own wire protocol — buys a copy of somebody else's protocol to keep in step.

So the socket stays on the host and only the COMMAND crosses, which is the
shape the MCP bridge already uses. A box gets a stand-in at herdr's own path;
it sends the arguments it was called with, the host runs the real binary and
sends back what it printed.

The host end is also where a box can be held to less than everything. Through
that socket `herdr agent read` shows any agent's screen, `send-keys` types
into any of them, and `pane process-info` prints command lines that carry the
fleet's tokens. A box listing `herdr: ["tab", "pane"]` gets those and nothing
else; one that lists nothing gets no stand-in at all.
"""

import json
import os
import socket
import subprocess
import threading

from .. import config
from ..out import warn

# What a box may be given. The first word of the command decides, which is how
# herdr groups them itself: `herdr tab list`, `herdr agent read`.
ALL = "*"

STAND_IN = "\n".join([
    "#!/usr/bin/env python3",
    "# Written by the broker: herdr runs on the host; this carries the command.",
    "import json, socket, sys",
    "HOST, PORT, TOKEN = %(host)s, %(port)d, %(token)s",
    "ask = json.dumps({'token': TOKEN, 'argv': sys.argv[1:]}).encode() + b'\\n'",
    "addresses = socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)",
    "family, kind, proto, _, address = addresses[0]",
    "try:",
    "    with socket.socket(family, kind, proto) as conn:",
    "        conn.connect(address)",
    "        conn.sendall(ask)",
    "        conn.shutdown(socket.SHUT_WR)",
    "        body = b''",
    "        while True:",
    "            chunk = conn.recv(65536)",
    "            if not chunk:",
    "                break",
    "            body += chunk",
    "except OSError as error:",
    "    sys.stderr.write('herdr: the broker bridge is not answering (%%s)\\n' %% error)",
    "    raise SystemExit(1)",
    "said = json.loads(body.decode('utf-8', 'replace') or '{}')",
    "sys.stdout.write(said.get('out', ''))",
    "sys.stderr.write(said.get('err', ''))",
    "raise SystemExit(int(said.get('code', 1)))",
    "",
])


def _state_file(name):
    return os.path.join(config.CONFIG_DIR, "box", "herdr",
                        "%s.json" % name.replace("/", "_"))


def live(name):
    """The listener already serving this box, or None.

    Alive is not the same as ours: a state file outlives the process it
    describes and pids come round again, so a box wired to a recycled one
    would send its arguments to a stranger. The start time recorded when the
    listener came up settles it.
    """
    from ..mcpserve import started

    try:
        with open(_state_file(name)) as handle:
            state = json.load(handle)
        os.kill(state["pid"], 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    was = state.get("started")
    if was and started(state["pid"]) != was:
        return None
    return state


def _stop(state):
    """Put down a listener of ours, for a box whose rule has changed."""
    import signal

    try:
        os.kill(state["pid"], signal.SIGTERM)
    except (OSError, KeyError, TypeError):
        pass


def allowed(argv, groups):
    """Whether a box that may use `groups` is allowed to run this command."""
    for word in argv:
        if word.startswith("-"):
            continue  # a flag before the subcommand decides nothing
        return ALL in groups or word in groups
    # A bare `herdr` opens the interactive terminal, which inside a box has
    # nothing to draw on and would simply hang. Refused even where everything
    # else is allowed.
    return False


def _answer(conn, binary, groups):
    """One request: check the secret, run herdr, send back what it said."""
    with conn:
        conn.settimeout(20)
        try:
            asked = b""
            while not asked.endswith(b"\n") and len(asked) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                asked += chunk
            want = json.loads(asked.decode("utf-8", "replace"))
        except (OSError, ValueError):
            return
        import secrets as _secrets
        if not _secrets.compare_digest(str(want.get("token", "")), _answer.token):
            return  # not ours: say nothing, run nothing
        argv = [str(a) for a in (want.get("argv") or [])]
        if not allowed(argv, groups):
            said = {"out": "", "code": 1,
                    "err": "herdr: this box may run %s, not %s\n"
                           % (", ".join(sorted(groups)) or "nothing",
                              " ".join(argv[:1]) or "a bare herdr")}
        else:
            try:
                done = subprocess.run([binary] + argv, capture_output=True,
                                      text=True, timeout=120)
                said = {"out": done.stdout, "err": done.stderr, "code": done.returncode}
            except (OSError, subprocess.SubprocessError) as exc:
                said = {"out": "", "err": "herdr: %s\n" % exc, "code": 1}
        try:
            conn.sendall(json.dumps(said).encode())
        except OSError:
            pass


def serve(name, binary, groups):
    """Listen on loopback for one box, for as long as that box may call.

    It used to give up after an hour of silence. That looks thrifty and is
    wrong: the port and the secret are written INTO the stand-in when the box
    starts, so a listener that goes away leaves the box holding a dead port
    with no way to learn a new one. Measured: a box ran from the morning,
    first asked herdr in the afternoon and got "connection refused" — the
    listener had closed at 13:32, four hours before the question.

    Living on costs one idle process per box NAME, not per container, because
    mount() reuses the one it finds. That is a handful on a machine.
    """
    import secrets
    from ..mcpserve import started

    token = secrets.token_urlsafe(24)
    _answer.token = token
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    path = _state_file(name)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".new"
    with open(tmp, "w") as handle:
        json.dump({"port": port, "token": token, "pid": os.getpid(),
                   "started": started(os.getpid()), "groups": sorted(groups)},
                  handle)
    os.chmod(tmp, 0o600)  # it carries the connection secret
    os.replace(tmp, path)

    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            break
        threading.Thread(target=_answer, args=(conn, binary, groups),
                         daemon=True).start()
    try:
        os.remove(path)
    except OSError:
        pass
    return 0


def mount(provider, profile, name):
    """Give this box a stand-in for herdr, if it was told it may have one.

    Returns docker arguments. A box that declares nothing gets nothing, which
    is the default: reaching the terminal manager is a privilege, not a part
    of being a box.
    """
    import sys
    import time

    from .paths import _bind, expand, window_key

    want = (profile or {}).get("herdr")
    if not want:
        return []
    groups = [ALL] if want is True else [str(g) for g in want]

    # Ask the system where herdr is, rather than writing a path down here. A
    # literal is wrong twice over: it is one machine's layout, and it goes
    # stale the day the tool moves. PATH is extended with the usual place a
    # user installs into, because the broker may be started by something with
    # a bare PATH.
    import shutil

    where = os.pathsep.join([os.environ.get("PATH", ""), expand("~/.local/bin"),
                             "/opt/homebrew/bin", "/usr/local/bin"])
    binary = shutil.which("herdr", path=where)
    if not binary:
        warn("this box asks for herdr, which is not on the path (%s)" % where)
        return []

    state = live(name)
    # A listener keeps the rule it was started with. Now that it also lives as
    # long as the machine does, a rule the owner NARROWS in boxes.json would
    # otherwise never take effect — the old listener would go on answering
    # under the wider one. So a changed rule replaces the listener. Boxes
    # already running hold the old port and lose herdr, which is the right
    # direction to fail when access is being taken away.
    if state and sorted(state.get("groups") or []) != sorted(groups):
        _stop(state)
        state = None
    if not state:
        try:
            subprocess.Popen(
                [sys.executable, "-m", "broker.box.herdr", "serve", name, binary]
                + groups,
                cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            warn("could not start the herdr bridge: %s" % exc)
            return []
        for _ in range(50):
            state = live(name)
            if state:
                break
            time.sleep(0.1)
    if not state:
        warn("the herdr bridge did not come up — the box will not reach it")
        return []

    from .mcp import HOST_GATEWAY
    body = STAND_IN % {"host": repr(HOST_GATEWAY), "port": state["port"],
                       "token": repr(state["token"])}
    directory = os.path.join(config.CONFIG_DIR, "box", "shims", "herdr")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "herdr-%s" % window_key())
    tmp = path + ".new"
    with open(tmp, "w") as handle:
        handle.write(body)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return _bind(path, binary, "ro")


def main(argv=None):
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == "serve":
        return serve(argv[1], argv[2], argv[3:] or [ALL])
    sys.stderr.write("usage: herdr serve <box> <binary> [group...]\n")
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
