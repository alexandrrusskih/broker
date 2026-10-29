"""A browser on the host, opened from inside a box.

An agent in a box meets a login wall — IAP, OAuth, a console that wants a
human — and has nowhere to put a browser. The container has no display, no
`open`, and the browser the person is already logged into is on the host,
with the host's cookies. Chromium inside the box is a different browser with
an empty profile, and Xvfb is a display nobody is looking at.

So the browser stays on the host and only the URL crosses, which is the shape
box/herdr.py already uses for the terminal manager — they share box/bridge.py,
because they are the same door. A box gets a stand-in at `open` and
`xdg-open`; it sends the one argument it was called with, the host opens it in
the person's own browser, and the box is told what happened. The box never
gets the browser, the profile, the cookies or a command — it gets to ASK, and
the host decides.

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

import os
import subprocess
import sys
import time

from .. import config
from . import bridge

KIND = "browser"
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

# Where the stand-in lands. A Linux container has neither, so each is a file
# docker creates and this mounts over — and both names are reached for: `open`
# is the habit of a person on a mac, `xdg-open` is what a tool on Linux calls.
STAND_IN_PATHS = ("/usr/bin/open", "/usr/bin/xdg-open")


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
    """Whether a box may open this address, and why not when it may not.

    Returns (True, host) or (False, reason). Everything about the answer comes
    from the URL as parsed, never from the string: an allowlist a caller can
    get past by spelling the host differently is decoration.
    """
    from urllib.parse import urlsplit

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


def _open(name, argv, hosts, app, recent):
    """The answer to one request, with nothing of the socket in it."""
    if len(argv) != 1:
        _note(name, " ".join(argv) or "-", "refused")
        return {"out": "", "code": 1,
                "err": "open: open takes exactly one URL (got %d arguments)\n" % len(argv)}
    url = argv[0]
    ok, why = allowed(url, hosts)
    if not ok:
        _note(name, url, "refused")
        return {"out": "", "err": "open: %s\n" % why, "code": 1}
    now = time.monotonic()
    recent[:] = [t for t in recent if now - t < 3600]
    if recent and now - recent[-1] < EVERY_SECONDS:
        _note(name, url, "too-fast")
        return {"out": "", "code": 1,
                "err": "open: one window at a time — try again in a moment\n"}
    if len(recent) >= PER_HOUR:
        _note(name, url, "too-many")
        return {"out": "", "code": 1,
                "err": "open: %d windows this hour is enough; ask the owner\n" % PER_HOUR}
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
        return {"out": done.stdout, "code": done.returncode,
                "err": done.stderr or "open: the host refused to open it\n"}
    recent.append(now)
    _note(name, url, "opened")
    return {"out": "opened on the host in %s: %s\n" % (app, url), "err": "", "code": 0}


def serve(name, app, hosts):
    """Listen for one box, opening what it is allowed to open."""
    recent = []
    return bridge.serve(KIND, name, lambda argv: _open(name, argv, hosts, app, recent),
                        {"app": app, "hosts": sorted(hosts)})


def mount(profile, name):
    """Give this box a stand-in for `open`, if it was told it may have one.

    Returns docker arguments. A box that declares nothing gets nothing: the
    person's own browser session is not part of being a box.
    """
    asked = rule(profile)
    if not asked:
        return []
    hosts, app = asked
    return bridge.mount(KIND, name, "broker.box.browser", [app] + hosts,
                        {"app": app, "hosts": sorted(hosts)},
                        "open", STAND_IN_PATHS, "open")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == "serve":
        return serve(argv[1], argv[2], argv[3:])
    sys.stderr.write("usage: browser serve <box> <app> <host>...\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
