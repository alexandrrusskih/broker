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

What a box CAN still do is name an id of the same harness belonging to another
window. It is refused for exactly one case: a run whose id this process CHOSE
before the container started, which is claude on a fresh chat. Then the report
has to match it. Everywhere else the id is the harness's own to invent or to
pick — codex, agy, opencode always, and claude too when the person resumes
through its picker instead of naming a chat, because then nothing out here ever
learns which one they chose. Those reports are accepted on their shape alone.
That is the boundary, and it is written down rather than hidden.
"""

import json
import os
import re
import threading

from .. import config
from ..out import die, warn
from . import herdr
from .paths import _bind, window_key


ROOT = os.path.join(config.CONFIG_DIR, "box", "reports")
# The name the hook reads. Chosen by the bus side; broker only has to agree.
ENV = "AGNTBUS_SESSION_REPORT_DIR"
NAME = "session.json"
# What the hook calls a harness against what the manager and the broker call it.
# The bus hook for agy reports "antigravity"; the canonical label is "agy", and
# a report under the other name is simply dropped on arrival.
AGENTS = {"antigravity": "agy"}

# The shape every harness's id has in common. Only a shape — see the module
# docstring for what it does and does not prove.
ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

POLL = 0.5


def directory(box):
    """One directory per box and window, as the launcher sees it."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", box)
    return os.path.join(ROOT, safe, window_key())


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
        try:
            os.unlink(os.path.join(host, NAME))
        except FileNotFoundError:
            pass
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
    """It goes when the box does: a left report names a chat that has ended."""
    import shutil

    shutil.rmtree(directory(box), ignore_errors=True)


def _accept(raw, provider, pinned):
    """The id in this report, or None and the reason it was refused."""
    try:
        record = json.loads(raw)
    except ValueError:
        return None, "it is not JSON (a half-written file, most likely)"
    if not isinstance(record, dict):
        return None, "it is not an object"
    said = record.get("agent")
    said = AGENTS.get(said, said)
    # Compared, not read: this process started the harness and knows which one.
    if said != provider.NAME:
        return None, "it claims to be %r and this box runs %s" % (record.get("agent"), provider.NAME)
    session = record.get("id")
    if not isinstance(session, str) or not ID.match(session):
        return None, "the id is not an id"
    if pinned and session != pinned:
        return None, "the id is not the one this run was given"
    return session, ""


class Watcher:
    """Reads one file, for as long as one container lives."""

    def __init__(self, box, provider, pinned):
        self.box, self.provider, self.pinned = box, provider, pinned
        self.pane = os.environ.get("HERDR_PANE_ID")
        self.path = os.path.join(directory(box), NAME)
        self.sent = None
        self.refused = set()
        self._seen = None
        self._mark = None
        self._stop = threading.Event()
        self._thread = None

    def announce(self, session):
        """Report an id this process already knows, before anything starts.

        The hook only fires on a bus call, so a resumed box would say nothing
        until its agent happened to use the bus — and after a cold restart that
        is exactly when the manager needs to know. The id is in the arguments
        this run was started with, which is a fact, not a guess from file times.
        """
        if not (self.pane and session) or session == self.sent:
            return
        taken, why = herdr.tell(self.pane, self.provider, self.box, session)
        if taken:
            self.sent = session
        elif why not in self.refused:
            self.refused.add(why)
            warn("the manager did not take the '%s' box's session: %s" % (self.box, why))

    def start(self):
        if not self.pane:
            return self  # nothing out here to report to
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _read(self):
        """The file as one piece, or None. Partial reads are not reports."""
        try:
            with open(self.path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def _changed(self):
        """Whether the file is worth opening. One stat, not one read.

        The hook rewrites this on EVERY bus call, which for a talkative agent is
        often, and almost always with the id it wrote last time. A tick that
        only stats costs nothing; reading and parsing on each one would be work
        done to reach the same answer.
        """
        try:
            info = os.stat(self.path)
        except OSError:
            return False
        mark = (info.st_mtime_ns, info.st_size, info.st_ino)
        if mark == self._mark:
            return False
        self._mark = mark
        return True

    def _loop(self):
        while not self._stop.wait(POLL):
            if not self._changed():
                continue
            raw = self._read()
            if raw is None or raw == self._seen:
                continue
            self._seen = raw
            session, why = _accept(raw.decode("utf-8", "replace"), self.provider, self.pinned)
            if session is None:
                if why not in self.refused:
                    self.refused.add(why)
                    warn("the '%s' box reported a session and it was refused: %s" % (self.box, why))
                continue
            self.announce(session)

    def stop(self):
        """Stop reading, and say if nothing ever came."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.pane and self.sent is None:
            warn("the '%s' box never reported its session — it will not wake by itself" % self.box)
