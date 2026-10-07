"""Folding a session written inside a box back into the harness outside.

Split out of run.py. The lock here is the reason this is its own file: two steps
in a different process, and a box that copies a database between them gets one
that says the session is archived.
"""

import fcntl
import os
import shlex
import subprocess
import sys
import time

from .. import config
from ..out import warn
from .sessions import _private_store


SYNC_LOG = os.path.join(config.CONFIG_DIR, "box", "sync.log")


class _StoreLock:
    """Held while a harness's databases are copied, and while one is written.

    Registering a session out here takes two steps, and between them the
    session is archived. A box starting in that instant copied a database that
    said so, and then refused to reopen its own session — "Failed to unarchive
    session" — over a thread this machine considered perfectly live. That is
    the only reason folding back was switched off.

    The two are not in the same process, or even the same run, so the lock is a
    file: whoever copies waits for whoever registers, and the other way round.
    """

    def __init__(self, provider):
        self.path = os.path.join(config.CONFIG_DIR, "box", "%s-store.lock" % provider.NAME)
        self.handle = None

    def __enter__(self):
        import fcntl

        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            self.handle = open(self.path, "a+")
            fcntl.flock(self.handle, fcntl.LOCK_EX)
        except OSError:
            # Never fatal: without the lock this is what it was before.
            self.handle = None
        return self

    def __exit__(self, *_exc):
        if self.handle is not None:
            try:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_UN)
            except OSError:
                pass
            self.handle.close()
            self.handle = None
        return False
def _sync_back(provider, session, env=None, name=None):
    """Put what the box wrote back into the history out here — in the background.

    A box works on its own clone of the harness's databases, because sharing one
    SQLite file across the container boundary tears it. The work itself is in
    the session file, which IS shared — so the harness is asked to read that
    session back into its history, which is what the command exists for. Merging
    its tables by hand would be us guessing at someone else's schema.

    Waited for, this held the prompt for several seconds every time: the harness
    looks through every session it has to find the one named, and there are
    thirteen thousand of them here. Nothing downstream depends on it having
    finished — the session file is the record, the database is a view of it — so
    it is started and left to run, with its output kept in case it fails.
    """
    template = getattr(provider, "BOX_SYNC", None)
    if not template:
        return
    from ..run import real_bin

    binary = real_bin(provider)
    if not binary:
        return
    # One command, or several to run in order — a harness may need more than a
    # single call to take a session into its history.
    steps = template if isinstance(template[0], (list, tuple)) else (template,)
    # Where the box's own copy of this harness's state ended up. A harness
    # whose sessions live in one database has nothing to fold back WITHOUT it:
    # the record is in there, not in a file the host can already see.
    store = _private_store(provider, name)
    fields = {"session": session, "bin": shlex.quote(binary),
              "store_parent": shlex.quote(os.path.dirname(store)) if store else ""}

    # A harness whose sessions live in one database cannot be folded back by
    # copying files: the whole history is in that one file, and the box's copy
    # would overwrite everything done outside while it ran. Such a harness
    # spells the fold as a shell line instead, reading its own copy and asking
    # itself to import the session — see providers/opencode.py.
    shell = getattr(provider, "BOX_SYNC_SHELL", None)
    if shell:
        if not store:
            return
        script = shell % fields
        by_hand = script
        argvs = None
    else:
        argvs = [[binary] + [part % fields for part in step] for step in steps]
    if argvs is not None:
        by_hand = " && ".join(
            " ".join([provider.BIN] + [part % fields for part in step])
            for step in steps)
    try:
        os.makedirs(os.path.dirname(SYNC_LOG), mode=0o700, exist_ok=True)
        log = open(SYNC_LOG, "a")
    except OSError:
        log = subprocess.DEVNULL
    try:
        log.write("\n=== %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), by_hand))
        log.flush()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        # Its own session, so quitting the terminal does not take it with it.
        # Chained through a shell rather than started one by one, because
        # nothing here waits: the steps must still run in order after this
        # process is gone.
        if argvs is not None:
            script = " && ".join(" ".join(shlex.quote(a) for a in argv) for argv in argvs)

        # Under the same lock the copying takes. These steps leave the session
        # archived in between, and a box copying the database right then would
        # carry that state into a container which then could not reopen its own
        # work — which is why folding back was switched off before. The child
        # holds the lock, because this process does not wait for it.
        script = "%s %s %s %s" % (
            shlex.quote(sys.executable),
            shlex.quote(os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdlock.py")),
            shlex.quote(_StoreLock(provider).path),
            script,
        )
        subprocess.Popen(["/bin/sh", "-c", script], env={**os.environ, **(env or {})},
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # Not fatal: the session file is intact and the command can be run again
        # by hand. Say which one, so it can be.
        warn("could not fold this session back into the history (%s) — run: %s"
             % (exc, by_hand))
    finally:
        if log is not subprocess.DEVNULL:
            try:
                log.close()
            except OSError:
                pass
