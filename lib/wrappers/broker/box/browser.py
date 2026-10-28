"""A browser on the host, opened from inside a box.

An agent in a box meets a login wall — IAP, OAuth, a console that wants a
human — and has nowhere to put a browser. The container has no display, no
`open`, and the browser the person is already logged into is on the host,
with the host's cookies. Chromium inside the box is a different browser with
an empty profile, and Xvfb is a display nobody is looking at.

So the browser stays on the host and only the URL crosses, which is the shape
box/herdr.py already uses for the terminal manager. A box gets a stand-in at
`open` and `xdg-open`; it sends the one argument it was called with, the host
opens it in the person's own browser, and the box is told what happened. The
box never gets the browser, the profile, the cookies or a command — it gets
to ASK, and the host decides.

Deciding is the point of the allowlist. A URL opened in a logged-in session is
not a passive thing: a plain GET can change state, and a session that reaches
an admin console reaches it as the person. So this is off unless a box asks
for it, and a box may be held to the hosts it actually needs:

    "finik": {
      "rw": ["~/Projects/finik"],
      "browser": ["admin-staging.finik.ai", ".finik.ai"]
    }

    true          any http/https address
    ["host", …]   those hosts; an entry starting with "." matches subdomains
    {"app": …}    the same, plus which browser to open

Every request is written to ~/.config/broker/box/browser.log, allowed or
refused, because an agent that opens a hundred tabs in somebody's face should
be answerable for it afterwards.
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

ANY = "*"
# The browser a person means by "my session": the one they are logged in to.
# Overridable per box, because a machine may keep its work in another one.
DEFAULT_APP = "Google Chrome"
# One at a time and not many in an hour. A loop that asks for a tab every
# iteration is the failure this exists to survive: the person is sitting in
# front of the window it opens into, and cannot work while it fills up.
EVERY_SECONDS = 2.0
PER_HOUR = 30
LOG = os.path.join(config.CONFIG_DIR, "box", "browser.log")

STAND_IN = "\n".join([
    "#!/usr/bin/env python3",
    "# Written by the broker: the browser runs on the host; this carries the URL.",
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
    "    sys.stderr.write('open: the broker bridge is not answering (%%s)\\n' %% error)",
    "    raise SystemExit(1)",
    "said = json.loads(body.decode('utf-8', 'replace') or '{}')",
    "sys.stdout.write(said.get('out', ''))",
    "sys.stderr.write(said.get('err', ''))",
    "raise SystemExit(int(said.get('code', 1)))",
    "",
])

# Where the stand-in lands. A Linux container has neither, so each is a file
# docker creates and this mounts over — and both names are reached for: `open`
# is the habit of a person on a mac, `xdg-open` is what a tool on Linux calls.
STAND_IN_PATHS = ("/usr/bin/open", "/usr/bin/xdg-open")


def _state_file(name):
    return os.path.join(config.CONFIG_DIR, "box", "browser",
                        "%s.json" % name.replace("/", "_"))


def rule(profile):
    """What a box asked for: (hosts it may open, the browser to open them in).

    None when it asked for nothing, which is every box that does not say so.
    """
    want = (profile or {}).get("browser")
    if not want:
        return None
    app = DEFAULT_APP
    if isinstance(want, dict):
        app = str(want.get("app") or DEFAULT_APP)
        want = want.get("allow", True)
        if not want:
            return None
    if want is True:
        return [ANY], app
    if isinstance(want, str):
        want = [want]
    return [str(host).strip().lower() for host in want if str(host).strip()], app


def allowed(url, hosts):
    """Whether this box may open this address, and why not when it may not.

    Returns (True, host) or (False, reason). Everything about the answer comes
    from the URL as parsed, never from the string: a box that could get a
    refusal past this by writing the address differently would make the
    allowlist decoration.
    """
    try:
        from urllib.parse import urlsplit
    except ImportError:  # pragma: no cover - python 2 has it elsewhere
        from urlparse import urlsplit
    if not url or url.startswith("-"):
        return False, "open takes one URL, not a flag"
    if len(url) > 2000:
        return False, "that URL is too long to be one"
    parts = urlsplit(url)
    # http and https only. `file:` reads the host's disk, `data:` and
    # `javascript:` run in whatever origin the window is on, and none of them
    # is a page somebody meant to be shown.
    if parts.scheme not in ("http", "https"):
        return False, "only http and https are opened, not '%s:'" % (parts.scheme or url[:20])
    host = (parts.hostname or "").lower()
    if not host:
        return False, "that URL names no host"
    if "@" in parts.netloc:
        return False, "a URL carrying credentials is not opened"
    for entry in hosts:
        if entry == ANY:
            return True, host
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True, host
        elif host == entry:
            return True, host
    return False, "this box may open %s, not %s" % (", ".join(hosts), host)


def _note(name, url, verdict):
    """One line per request, kept whatever the answer was."""
    try:
        os.makedirs(os.path.dirname(LOG), mode=0o700, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write("%s\t%s\t%s\t%s\n"
                         % (time.strftime("%Y-%m-%dT%H:%M:%S"), name, verdict, url))
    except OSError:
        pass


def _command(app, url):
    """How this machine opens a URL in a named browser."""
    if sys.platform == "darwin":
        return ["open", "-a", app, "--", url]
    # Linux has no way to name the browser that does not depend on which one it
    # is, so the machine's own handler decides and `app` is not used.
    return ["xdg-open", url]


def _answer(conn, name, hosts, app, recent):
    """One request: check the secret, check the URL, open it, say what happened."""
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
            return  # not ours: say nothing, open nothing
        argv = [str(a) for a in (want.get("argv") or [])]
        said = _decide(name, argv, hosts, app, recent)
        try:
            conn.sendall(json.dumps(said).encode())
        except OSError:
            pass


def _decide(name, argv, hosts, app, recent):
    """The answer to one request, with nothing of the socket in it."""
    if len(argv) != 1:
        reason = "open takes exactly one URL (got %d arguments)" % len(argv)
        _note(name, " ".join(argv) or "-", "refused")
        return {"out": "", "err": "open: %s\n" % reason, "code": 1}
    url = argv[0]
    ok, why = allowed(url, hosts)
    if not ok:
        _note(name, url, "refused")
        return {"out": "", "err": "open: %s\n" % why, "code": 1}
    now = time.monotonic()
    recent[:] = [t for t in recent if now - t < 3600]
    if recent and now - recent[-1] < EVERY_SECONDS:
        _note(name, url, "too-fast")
        return {"out": "", "err": "open: one window at a time — try again in a moment\n",
                "code": 1}
    if len(recent) >= PER_HOUR:
        _note(name, url, "too-many")
        return {"out": "", "err": "open: %d windows this hour is enough; ask the owner\n"
                                  % PER_HOUR, "code": 1}
    try:
        done = subprocess.run(_command(app, url), capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        _note(name, url, "failed")
        return {"out": "", "err": "open: %s\n" % exc, "code": 1}
    if done.returncode != 0:
        _note(name, url, "failed")
        # The usual reason on a mac is that nobody is logged in to the screen,
        # and `open` says so itself. Passed through rather than summarised.
        return {"out": done.stdout, "err": done.stderr or "open: the host refused to open it\n",
                "code": done.returncode}
    recent.append(now)
    _note(name, url, "opened")
    return {"out": "opened on the host in %s: %s\n" % (app, url), "err": "", "code": 0}


def live(name):
    """The listener already serving this box, or None.

    Alive is not the same as ours — see box/herdr.py, which learned it the
    hard way: a state file outlives its process and pids come round again.
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


def serve(name, app, hosts):
    """Listen on loopback for one box, for as long as that box may ask.

    It outlives the container for the same reason the herdr bridge does: the
    port and the secret are written INTO the stand-in when the box starts, so
    a listener that goes away leaves the box holding a dead port and no way to
    learn a new one.
    """
    import secrets
    from ..mcpserve import started

    token = secrets.token_urlsafe(24)
    _answer.token = token
    recent = []
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
                   "started": started(os.getpid()), "app": app,
                   "hosts": sorted(hosts)}, handle)
    os.chmod(tmp, 0o600)  # it carries the connection secret
    os.replace(tmp, path)

    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            break
        threading.Thread(target=_answer, args=(conn, name, hosts, app, recent),
                         daemon=True).start()
    try:
        os.remove(path)
    except OSError:
        pass
    return 0


def mount(profile, name):
    """Give this box a stand-in for `open`, if it was told it may have one.

    Returns docker arguments. A box that declares nothing gets nothing: the
    person's own browser session is not part of being a box.
    """
    from .paths import _bind, window_key

    asked = rule(profile)
    if not asked:
        return []
    hosts, app = asked

    state = live(name)
    # A listener keeps the rule it started with, and it lives longer than the
    # container does, so a rule the owner NARROWS would otherwise go on being
    # answered under the old one. A changed rule replaces the listener. Boxes
    # already running hold the old port and lose `open`, which is the right
    # direction to fail when access is being taken away.
    if state and (sorted(state.get("hosts") or []) != sorted(hosts)
                  or state.get("app") != app):
        _stop(state)
        state = None
    if not state:
        try:
            subprocess.Popen(
                [sys.executable, "-m", "broker.box.browser", "serve", name, app] + hosts,
                cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            warn("could not start the browser bridge: %s" % exc)
            return []
        for _ in range(50):
            state = live(name)
            if state:
                break
            time.sleep(0.1)
    if not state:
        warn("the browser bridge did not come up — the box will not reach it")
        return []

    from .mcp import HOST_GATEWAY
    body = STAND_IN % {"host": repr(HOST_GATEWAY), "port": state["port"],
                       "token": repr(state["token"])}
    directory = os.path.join(config.CONFIG_DIR, "box", "shims", "browser")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "open-%s" % window_key())
    tmp = path + ".new"
    with open(tmp, "w") as handle:
        handle.write(body)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    args = []
    for target in STAND_IN_PATHS:
        args += _bind(path, target, "ro")
    return args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == "serve":
        return serve(argv[1], argv[2], argv[3:])
    sys.stderr.write("usage: browser serve <box> <app> <host>...\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
