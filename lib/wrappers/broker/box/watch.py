"""Reading the one file a box writes, and keeping at it until it lands.

Split out of box/report.py, which is about the directory itself. This is the
reading half: what a report has to be before it is believed, and the retrying
that a manager restart needs.

WHAT IS BELIEVED, AND HOW FAR. The pane is the boundary, and it is not in the
report: it comes from this process's own environment, and this process reports
for its own pane and no other. So the worst a lying box achieves is to point
ITS OWN pane at another chat of the same harness — never somebody else's pane.

On top of that there is one piece of proof, and it covers the FIRST report
only. When this process chose the id before the container started — claude on a
fresh chat — the first report has to match it. After that it must not: the
person types /clear, or resumes another chat inside the one they have, and the
harness is then legitimately in a chat nobody out here named. ph found this as
a blocker, and it was the real cost of being strict: the report was refused, so
the pane kept the first chat for ever and a cold restart reopened the wrong
one.

So the pinned id is proof of a beginning, not a lease. Once it has been
accepted, a later id from the same box is taken on its shape — which is exactly
where codex, agy and opencode stand all the time, and where claude stands too
whenever the person resumes through its own picker and nothing out here ever
learns which chat they chose. Written down rather than hidden.
"""

import json
import os
import re
import stat
import threading
import time

from ..out import warn
from . import herdr, report


POLL = 0.5

# A report is under two hundred bytes. The box writes this file, so the size is
# the box's choice and the limit is ours.
LIMIT = 1024

# What the hook calls a harness against what the manager and the broker call it.
# The bus hook for agy reports "antigravity"; the canonical label is "agy", and
# a report under the other name is simply dropped on arrival.
AGENTS = {"antigravity": "agy"}

# The shape every harness's id has in common. Only a shape — see above for what
# it does and does not prove.
ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def accept(raw, provider, pinned):
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
        # Only until the pinned id itself has landed; see the module docstring.
        # A box that names another chat BEFORE proving it is the box that was
        # given this one is refused, and a box whose first report never lands
        # never gets past this line either — fail-closed stays fail-closed.
        return None, "the id is not the one this run was given"
    return session, ""


class Watcher:
    """Reads one file, for as long as one container lives."""

    def __init__(self, box, provider, pinned):
        self.box, self.provider, self.pinned = box, provider, pinned
        self.pane = os.environ.get("HERDR_PANE_ID")
        self.path = os.path.join(report.directory(box), report.NAME)
        self.sent = None
        self.pending = None
        self.refused = set()
        self._retry_at = 0.0
        self._backoff = 1.0
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
        if session != self.pending:
            self.pending, self._retry_at, self._backoff = session, 0.0, 1.0
        self._try()

    def _try(self):
        """One attempt at the id waiting to go, if it is time to make one.

        Kept trying, because the first version gave up without knowing it had.
        A failure left `sent` unset while the file's bytes were remembered as
        seen, so the same id was never offered again — and the one failure that
        matters is the manager being restarted, which is exactly when it stops
        answering for a moment and then wants to be told everything.

        Backed off rather than retried every tick: a dead socket costs a
        connect timeout, and spending that twice a second would stall the
        reading as well as waste the wait.
        """
        if not self.pending or self.pending == self.sent:
            return
        now = time.monotonic()
        if now < self._retry_at:
            return
        taken, why = herdr.tell(self.pane, self.provider, self.box, self.pending)
        if taken:
            # Whatever was just taken can only have been the pinned id while
            # one was outstanding — accept() allows nothing else through. So
            # this is the moment the beginning is proved, and a /clear in the
            # same box is a chat switch rather than an impostor.
            self.sent, self.pending, self._backoff = self.pending, None, 1.0
            self.pinned = None
            return
        self._retry_at = now + self._backoff
        self._backoff = min(self._backoff * 2, 30.0)
        if why not in self.refused:
            self.refused.add(why)
            warn("the manager did not take the '%s' box's session: %s" % (self.box, why))

    def start(self):
        if not self.pane:
            return self  # nothing out here to report to
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _read(self):
        """The file, bounded, or None. Partial reads are not reports.

        Bounded because a container writes this and the broker reads it: an
        unbounded read of a file somebody else controls is a box that can take
        this process's memory with it. A report is under two hundred bytes, so
        one kilobyte is room enough for the format to grow and small enough that
        it cannot matter.

        O_NOFOLLOW for the same reason from the other side: the box can put a
        symlink here, and following one would read whatever it points at —
        inside its own mounts, but still a file it chose rather than the report
        it was asked for.
        """
        try:
            handle = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                return None
            raw = os.read(handle, LIMIT + 1)
        except OSError:
            return None
        finally:
            os.close(handle)
        if len(raw) > LIMIT:
            return b"too big"  # refused below, by the same path as any nonsense
        return raw

    def _changed(self):
        """Whether the file is worth opening. One lstat, not one read.

        The hook rewrites this on EVERY bus call, which for a talkative agent is
        often, and almost always with the id it wrote last time. A tick that
        only stats costs nothing; reading and parsing on each one would be work
        done to reach the same answer.
        """
        try:
            info = os.lstat(self.path)  # a symlink here is not followed, even to stat it
        except OSError:
            return False
        mark = (info.st_mtime_ns, info.st_size, info.st_ino)
        if mark == self._mark:
            return False
        self._mark = mark
        return True

    def _loop(self):
        while not self._stop.wait(POLL):
            if self._changed():
                raw = self._read()
                if raw is not None and raw != self._seen:
                    self._seen = raw
                    self._take(raw)
            # An id already validated but not yet taken, whatever the file does.
            self._try()

    def _take(self, raw):
        session, why = accept(raw.decode("utf-8", "replace"), self.provider, self.pinned)
        if session is None:
            if why not in self.refused:
                self.refused.add(why)
                warn("the '%s' box reported a session and it was refused: %s" % (self.box, why))
            return
        self.announce(session)

    def stop(self):
        """Stop reading, and say if nothing ever came."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.pane and self.sent is None:
            warn("the '%s' box never reported its session — it will not wake by itself" % self.box)
