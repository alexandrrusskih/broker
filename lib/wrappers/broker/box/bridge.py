"""One loopback listener per box, for the things that stay on the host.

Two of a box's doors turned out to be the same door. The terminal manager
cannot cross because its client is a macOS binary and a Linux container has
nothing to run it with. A browser cannot cross because the display and the
signed-in profile are the host's. Both are answered the same way: the thing
stays here, a stand-in goes into the box, and one request crosses.

Everything AROUND the decision is here — the loopback listener, the secret
written into the stand-in, the state file saying which listener belongs to
which box, and the rule that listener was started with. The decision itself is
not: each bridge is handed the arguments and says what it did with them. That
line is where the two differ and the only place they should.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

from .. import config
from ..out import warn

# The box's end. It carries the request and nothing else: no command runs on
# this side of the wall, which is the whole point of the arrangement.
STAND_IN = "\n".join([
    "#!/usr/bin/env python3",
    "# Written by the broker: the real thing runs on the host; this carries the ask.",
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
    "    sys.stderr.write('%(prefix)s: the broker bridge is not answering (%%s)\\n' %% error)",
    "    raise SystemExit(1)",
    "said = json.loads(body.decode('utf-8', 'replace') or '{}')",
    "sys.stdout.write(said.get('out', ''))",
    "sys.stderr.write(said.get('err', ''))",
    "raise SystemExit(int(said.get('code', 1)))",
    "",
])


def state_file(kind, name):
    return os.path.join(config.CONFIG_DIR, "box", kind,
                        "%s.json" % name.replace("/", "_"))


def live(kind, name):
    """The listener already serving this box, or None.

    Alive is not the same as ours: a state file outlives the process it
    describes and pids come round again, so a box wired to a recycled one
    would send its arguments to a stranger. The start time recorded when the
    listener came up settles it.
    """
    from ..mcpserve import started

    try:
        with open(state_file(kind, name)) as handle:
            state = json.load(handle)
        os.kill(state["pid"], 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    was = state.get("started")
    if was and started(state["pid"]) != was:
        return None
    return state


def stop(state):
    """Put down a listener of ours, for a box whose rule has changed."""
    import signal

    try:
        os.kill(state["pid"], signal.SIGTERM)
    except (OSError, KeyError, TypeError):
        pass


def _answer(conn, token, handler):
    """One request: check the secret, hand it over, send back what was said."""
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
        import secrets

        if not secrets.compare_digest(str(want.get("token", "")), token):
            return  # not ours: say nothing, do nothing
        said = handler([str(a) for a in (want.get("argv") or [])])
        try:
            conn.sendall(json.dumps(said).encode())
        except OSError:
            pass


def serve(kind, name, handler, rule=None):
    """Listen on loopback for one box, for as long as that box may call.

    It outlives the container on purpose. Giving up after an idle hour looks
    thrifty and is wrong: the port and the secret are written INTO the stand-in
    when the box starts, so a listener that goes away leaves the box holding a
    dead port with no way to learn a new one. Measured on the herdr bridge: a
    box ran from the morning, first called in the afternoon and got "connection
    refused" — the listener had closed four hours before the question.

    Living on costs one idle process per box NAME, not per container, because
    mount() reuses the one it finds. That is a handful on a machine.
    """
    import secrets
    from ..mcpserve import started

    token = secrets.token_urlsafe(24)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    path = state_file(kind, name)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    state = {"port": port, "token": token, "pid": os.getpid(),
             "started": started(os.getpid())}
    state.update(rule or {})
    tmp = path + ".new"
    with open(tmp, "w") as handle:
        json.dump(state, handle)
    os.chmod(tmp, 0o600)  # it carries the connection secret
    os.replace(tmp, path)

    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            break
        threading.Thread(target=_answer, args=(conn, token, handler),
                         daemon=True).start()
    try:
        os.remove(path)
    except OSError:
        pass
    return 0


def mount(kind, name, module, argv, rule, shim, targets, prefix):
    """Start or find this box's listener, and give the box its stand-in.

    `rule` is what the listener is allowed to do, and it is compared with the
    one already running. A listener keeps the rule it was started with, and it
    lives longer than the container, so a rule the owner NARROWS in boxes.json
    would otherwise go on being answered under the old one. A changed rule
    replaces the listener: boxes already running hold the old port and lose the
    bridge, which is the right direction to fail when access is taken away.
    """
    from .mcp import HOST_GATEWAY
    from .paths import _bind, window_key

    state = live(kind, name)
    if state and any(state.get(key) != value for key, value in rule.items()):
        stop(state)
        state = None
    if not state:
        try:
            subprocess.Popen(
                [sys.executable, "-m", module, "serve", name] + list(argv),
                cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            warn("could not start the %s bridge: %s" % (kind, exc))
            return []
        for _ in range(50):
            state = live(kind, name)
            if state:
                break
            time.sleep(0.1)
    if not state:
        warn("the %s bridge did not come up — the box will not reach it" % kind)
        return []

    body = STAND_IN % {"host": repr(HOST_GATEWAY), "port": state["port"],
                       "token": repr(state["token"]), "prefix": prefix}
    directory = os.path.join(config.CONFIG_DIR, "box", "shims", kind)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "%s-%s" % (prefix, window_key()))
    tmp = path + ".new"
    with open(tmp, "w") as handle:
        handle.write(body)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    args = []
    for target in targets:
        args += _bind(path, target, "ro")
    return args
