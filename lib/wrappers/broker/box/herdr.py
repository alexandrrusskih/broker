"""The terminal manager, reachable from inside a box without its socket.

A box that could talk to herdr directly would need its unix socket, and
Docker Desktop does not carry one of those across the boundary of its virtual
machine: the mount is refused with "bind source path does not exist" against
a path that plainly exists. That is its limit, not ours.

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
    """The listener already serving this box, or None."""
    try:
        with open(_state_file(name)) as handle:
            state = json.load(handle)
        os.kill(state["pid"], 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return state


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
    """Listen on loopback for one box, until nobody comes back for an hour."""
    import secrets

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
                   "groups": sorted(groups)}, handle)
    os.chmod(tmp, 0o600)  # it carries the connection secret
    os.replace(tmp, path)

    listener.settimeout(3600)
    while True:
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            break  # the box is gone, or has not asked in an hour
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

    binary = expand("~/.local/bin/herdr")
    if not os.path.exists(binary):
        warn("this box asks for herdr, which is not installed at %s" % binary)
        return []

    state = live(name)
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
