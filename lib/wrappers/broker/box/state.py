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


def claim(box, provider, session):
    """Write it down before the container starts."""
    try:
        config.write_json(path(box), {
            "box": box,
            "window": window_key(),
            "harness": provider.NAME,
            "session": session or None,
            "pane": os.environ.get("HERDR_PANE_ID") or None,
            "workspace": os.environ.get("HERDR_WORKSPACE_ID") or None,
            "started_ms": int(time.time() * 1000),
        }, prefix=".state-")
    except OSError as exc:
        warn("could not note which pane this box belongs to (%s)" % exc)


def release(box):
    """It goes when the box does: a stale line names a chat that has ended."""
    try:
        os.unlink(path(box))
    except OSError:
        pass
