"""The one thing a box is allowed to say about itself: which chat is in it.

A terminal manager wakes a chat by pane. For a box it could not: the harness
runs inside a container, and nothing out here knows which conversation that is.
The bus hook inside the box DOES know — it already writes the id down. It wrote
it to $HOME, which in a box is a tmpfs broker mounts itself, so every report
ever written went into memory and died with the container.

So the box is given one directory from this machine, and that directory is the
whole of its authority. It can name its own chat in there. It cannot name a
pane: the pane comes from this process's own environment, and this process
reports for no other. Nothing is signed and nothing is checked, because there is
nothing to check — a mount reaches exactly one container, which is a stronger
statement than any secret a box could be told and then have read out of it.

What a box can still get wrong, and what is refused, is in box/watch.py with
the code that judges a report.
"""

import os
import re
import secrets
import shutil
import time

from .. import config
from ..out import die, warn
from .paths import _bind, window_key


ROOT = os.path.join(config.CONFIG_DIR, "box", "reports")
# The name the hook reads. Chosen by the bus side; broker only has to agree.
ENV = "AGNTBUS_SESSION_REPORT_DIR"
NAME = "session.json"

# A report is under two hundred bytes. The box writes this file, so the size is
# the box's choice and the limit is ours.


# One per LAUNCH, not per window. A cold restart of the manager overlaps the
# two: the pane is relaunched while the old broker is still in its `finally`,
# and with a path derived from the window alone the old run's cleanup deleted
# the new run's directory out from under it. Same process, same answer, so the
# command builder and the watcher still agree.
LAUNCH = "%d-%s" % (os.getpid(), secrets.token_hex(4))

# How long an orphan may sit before it is swept. Long enough that no live run is
# ever a candidate: a report directory is removed when its box ends, so anything
# left is from a run that was killed outright.
ORPHAN_AGE = 24 * 60 * 60


def directory(box, launch=None):
    """This launch's directory, as the launcher sees it."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", box)
    return os.path.join(ROOT, safe, window_key(), launch or LAUNCH)


def _sweep(box):
    """Drop directories left by runs that were killed before they could tidy.

    By age only. Anything else — a pid check, "the newest is mine" — would be a
    guess about somebody else's live run, and deleting a live run's directory is
    the bug this is here to avoid in the first place.
    """
    window = os.path.dirname(directory(box))
    try:
        names = os.listdir(window)
    except OSError:
        return
    now = time.time()
    for name in names:
        if name == LAUNCH:
            continue
        found = os.path.join(window, name)
        try:
            if now - os.path.getmtime(found) < ORPHAN_AGE:
                continue
        except OSError:
            continue
        shutil.rmtree(found, ignore_errors=True)


def flags(box):
    """Make this run's report directory, empty it, and mount it.

    A DIRECTORY, deliberately. The hook publishes by writing beside and renaming
    over, and a bind mount of a FILE pins one inode: after the first rename the
    mount points at something nothing links to, and inside the box the file has
    simply vanished. That is how ~/.claude.json was lost here once.

    Emptied first because only THIS launch counts. A report left behind by a run
    that crashed names a chat that has ended, and sending it would point the
    pane at a dead id — worse than saying nothing.
    """
    host = directory(box)
    try:
        os.makedirs(host, mode=0o700, exist_ok=True)
        os.chmod(host, 0o700)
        # Belt and braces: the directory is this launch's own, so there can be
        # nothing in it — unless a pid came round again on a machine that has
        # been up a very long time.
        try:
            os.unlink(os.path.join(host, NAME))
        except FileNotFoundError:
            pass
        _sweep(box)
    except OSError as exc:
        # In a managed pane this is fatal. The box would start, work, and never
        # be wakeable — and nothing on screen would say so until somebody tried
        # to wake it and nothing happened. Outside a pane there is nothing to
        # wake, so it is only worth saying.
        said = "the '%s' box cannot make its report directory: %s" % (box, exc)
        if os.environ.get("HERDR_PANE_ID"):
            die("%s — this pane could not wake it, so it is not started" % said)
        warn("%s — it will not wake by itself" % said)
        return []
    return ["-e", "%s=%s" % (ENV, host)] + _bind(host, host, "rw")


def release(box):
    """It goes when the box does: a left report names a chat that has ended.

    This launch's directory and no other. An ending run and a starting one
    overlap on every cold restart, and the ending one must not reach into the
    other's.
    """
    shutil.rmtree(directory(box), ignore_errors=True)
    # The window above it, only while it is empty: another launch may be in it.
    try:
        os.rmdir(os.path.dirname(directory(box)))
    except OSError:
        pass
