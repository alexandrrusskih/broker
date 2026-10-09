"""Which pane a running box belongs to, and which chat is in it.

A terminal manager wakes a chat by pane. For a box it cannot: the harness runs
inside a container, and nothing out here knows which conversation that is.

The launcher knows both halves for a pinned harness — the pane it was started
from, and the session id it gave the harness before the container began. So it
writes them down, on the host, in one small file per box and window, and removes
it when the box ends. Nothing is mounted and nothing in the box can write here:
a box that could name its own pane could be woken in another agent's place.

What is NOT here is the harness that only learns its own session id once it
starts talking — codex, agy, opencode. For those the id is simply absent, and
whoever wants to wake them needs it from the harness itself.
"""

import json
import os
import re
import time

from .. import config
from ..out import warn
from .paths import window_key


def root():
    """Where the state files live, asked EVERY time.

    Not a constant computed at import, for the reason box/report.py gives at
    its own root(): config.CONFIG_DIR is moved at run time, and a frozen value
    writes into the real home instead. The same mistake was here first.
    """
    return os.path.join(config.CONFIG_DIR, "box", "state")


def path(box):
    """One file per box and window."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", box)
    return os.path.join(root(), safe, window_key() + ".json")


def claim(box, provider, session, env=None, workdir=None):
    """Write it down before the container starts.

    cwd and config_home are here for the harness that is NOT told its id: they
    are what resolve() needs to find the session file it makes for itself.
    """
    home_env = getattr(provider, "HOME_ENV", None)
    try:
        config.write_json(path(box), {
            "box": box,
            "window": window_key(),
            "harness": provider.NAME,
            "session": session or None,
            "pane": os.environ.get("HERDR_PANE_ID") or None,
            "workspace": os.environ.get("HERDR_WORKSPACE_ID") or None,
            "cwd": workdir or os.path.realpath(os.getcwd()),
            "config_home": (env or {}).get(home_env) if home_env and home_env != "HOME" else None,
            "started_ms": int(time.time() * 1000),
        }, prefix=".state-")
    except OSError as exc:
        warn("could not note which pane this box belongs to (%s)" % exc)


def live():
    """Every box this machine has running, as the launchers noted them.

    One file per running box: written before the container starts, removed when
    it ends. No container is asked anything.
    """
    import glob as globmodule

    out = []
    for found in sorted(globmodule.glob(os.path.join(root(), "*", "*.json"))):
        try:
            with open(found, encoding="utf-8") as handle:
                out.append(json.load(handle))
        except (OSError, ValueError):
            continue
    return out


def resolve(entry):
    """What is known about this box's chat, and how — or nothing.

    Only exact sources: the id the launcher handed the harness before it
    started, or the one typed on the command line to resume. There is no third.

    A fallback by file time was tried here and withdrawn the same day. Every
    window writes its sessions into one directory, so two boxes started in the
    same second each see the other's file as "created after I began", and both
    are handed an id belonging to neither. box/start.py already says this in
    plain words about the same mistake made once before; codex-misc-p5 caught
    me repeating it.

    So for a harness that names its own session — codex, agy, opencode on a
    fresh run — this returns nothing, and the honest word for that is
    unsupported.
    """
    if entry.get("session"):
        return entry, entry.get("confirmed_by") or "argv"
    return entry, "unsupported: this harness names its own session"


def release(box):
    """It goes when the box does: a stale line names a chat that has ended."""
    try:
        os.unlink(path(box))
    except OSError:
        pass
