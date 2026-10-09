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


ROOT = os.path.join(config.CONFIG_DIR, "box", "state")


def path(box):
    """One file per box and window."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", box)
    return os.path.join(ROOT, safe, window_key() + ".json")


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
    for found in sorted(globmodule.glob(os.path.join(ROOT, "*", "*.json"))):
        try:
            with open(found, encoding="utf-8") as handle:
                out.append(json.load(handle))
        except (OSError, ValueError):
            continue
    return out


def resolve(entry):
    """Fill in the session for a harness that was not told its id.

    claude is given one before it starts, so there is nothing to do. The others
    make their own as they begin, and write it into the name of their session
    file — which the host already has. Returns (entry, how it was established).
    """
    if entry.get("session"):
        return entry, "argv"
    from ..providers import agy, claude, codex, opencode
    from .sessions import _session_since

    known = {p.NAME: p for p in (claude, codex, agy, opencode)}
    provider = known.get(entry.get("harness"))
    started = entry.get("started_ms")
    if not provider or not started:
        return entry, "unknown"
    env = {}
    home_env = getattr(provider, "HOME_ENV", None)
    if home_env and entry.get("config_home"):
        env[home_env] = entry["config_home"]
    found = _session_since(provider, entry.get("cwd") or "", started / 1000.0, env)
    if not found:
        return entry, "none yet"
    note = dict(entry)
    note["session"] = found
    return note, "session file written after the box started"


def release(box):
    """It goes when the box does: a stale line names a chat that has ended."""
    try:
        os.unlink(path(box))
    except OSError:
        pass
