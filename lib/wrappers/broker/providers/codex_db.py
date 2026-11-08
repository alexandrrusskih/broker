"""A session on disk: the error it ended on, and the databases that index it.

One set of databases per window, because this harness cannot share them.

The canonical home is passed in rather than imported: the provider module
declares it, and the provider module imports this one.
"""

import os
import re
import sqlite3


def session_error(path):
    """The last failure this session recorded, or None if it ended cleanly."""
    import json

    try:
        with open(path, "rb") as handle:
            # The tail only: these run to tens of megabytes, and what is wanted
            # is the final turn.
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 262144))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            payload = (json.loads(line) or {}).get("payload") or {}
        except ValueError:
            continue
        if payload.get("type") != "task_complete":
            continue
        # A turn that ended normally carries no error at all, and once one has
        # been found there is no point reading further back: older turns are
        # not what the person is standing in front of.
        message = ((payload.get("error") or {}).get("message") or "").strip()
        return message or None
    return None


# Where the databases go: one set per WINDOW, rather than one set for the
# machine.
#
# They are SQLite, and this harness has no BUSY retry: the log writer flushes
# every two seconds holding the write lock, and a second process arriving in
# that moment either hangs or is told the database is locked. Worse, a start
# is REFUSED outright when anything holds a write lock on the log database —
# the telemetry file gates the boot. Eight windows open on this machine and a
# day of it ends the way this one did: "database disk image is malformed", a
# state file that is no longer a database at all, and every session in the
# picker gone.
#
# It is not ours to fix and it is known upstream (openai/codex #20213, #35555,
# #30105, #44772); what everyone is told to do is give each instance its own.
# CODEX_HOME alone does not: the harness symlinks every database back into the
# canonical ~/.codex whatever that variable says — measured here, by putting
# real empty files there and watching one run replace them with links again.
# CODEX_SQLITE_HOME is the one it honours.
#
# What stays shared is what matters: the sessions themselves, the settings, the
# accounts. The databases are an index over those files — a fresh one filled
# itself from all 13,774 rollouts on first start, so nothing is lost by a
# window starting with none.
#
# In the home, on the same volume as the canonical databases. This used to be
# the big disk, to keep 600 MB per window out of the home — and that is what
# made every window pay for it. A clone needs ONE filesystem: the canonical
# set is on the internal volume, the big disk is another one, so clonefile
# could not be used and _seed_databases fell back to copying 600 MB across
# volumes, once per window. Measured here: cloning the 604 MB state database
# within the internal volume takes 0.02 s and no space, while the cross-volume
# copy ran at about 8 MB a minute against a machine busy with sixteen
# containers — a window sat on "Waiting for startup" for ten minutes and was
# not a sixth of the way through. The same mistake filled the big disk with 39
# GB in 52 directories, because over there every copy is a real one.
SQLITE_ENV = "CODEX_SQLITE_HOME"
#
# Under broker-box rather than broker: `broker remove` deletes ~/.cache/broker
# whole, and taking the indexes with it would buy back every slow start we just
# paid off. The slot files already live beside this, for the same reason.
SQLITE_BASE = os.path.expanduser("~/.cache/broker-box/codex-db")


def _device(path):
    """The volume `path` is on, looking up to the first parent that exists."""
    while True:
        try:
            return os.stat(path).st_dev
        except OSError:
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent


def _warn_if_other_volume(base, canonical):
    """Say so when the seed cannot be a clone, instead of starting slowly.

    clonefile(2) works within one filesystem. Put the window databases on a
    volume other than the canonical set and _seed_databases silently becomes a
    600 MB copy, once per window — which reads as the harness hanging on
    startup, with nothing to point at. Saying it costs one line and this cost
    three weeks of slow starts.
    """
    from ..out import warn

    here, there = _device(base), _device(canonical)
    if here is None or there is None or here == there:
        return
    warn("codex databases go to %s, which is not the volume %s is on — the "
         "600 MB seed cannot be cloned and is copied instead, once per window"
         % (base, canonical))


def harness_env(env, canonical, home_env):
    """Point this window's harness at its own databases."""
    # The stale links go whatever else happens here: they are what stops a box
    # from starting at all.
    _unlink_shared_databases(env.get(home_env), canonical)
    # Not forced: someone who set it meant it.
    if env.get(SQLITE_ENV):
        _share_threads(env[SQLITE_ENV], canonical)
        _repair_rollout_paths(env[SQLITE_ENV], canonical)
        return
    from ..box.paths import window_key

    base = SQLITE_BASE
    _warn_if_other_volume(base, canonical)
    own = os.path.join(base, window_key())
    _sweep_dead_windows(base, own)
    fresh = not os.path.isdir(own)
    try:
        os.makedirs(own, mode=0o700, exist_ok=True)
    except OSError:
        return  # the shared ones are worse, but they are better than no start
    if fresh:
        _seed_databases(own, canonical)
    env[SQLITE_ENV] = own
    _share_threads(own, canonical)
    _repair_rollout_paths(own, canonical)


SWEEP_AFTER_S = 600
# Five days, the same horizon the session archive uses: one rule to remember
# rather than two. Coming back to last week's window still finds its index;
# after that it is rebuilt, which costs a start and no data — the index is
# derived from the rollouts, and those are shared.
SWEEP_OLD_S = 5 * 24 * 3600


def _sweep_dead_windows(base, own):
    """Remove the databases of windows whose process group is gone.

    A window without a pane is keyed by its process group, and every run of the
    harness from a script is a new group — so each got its own seeded copy and
    nothing ever took one away. Measured on a Linux host running agent lanes:
    199 such directories in three days, 71 GB, all of dead groups.

    Only "pgid-" directories go, and only when no process is left in that group
    and nothing was written there for ten minutes: a pane-keyed one belongs to a
    window a person may come back to, and a group that still exists may still
    be writing. Anything that cannot be checked or removed is left alone.
    """
    import shutil
    import time

    try:
        names = os.listdir(base)
    except OSError:
        return
    now = time.time()

    def untouched(path):
        """Seconds since anything in here was written, or None if unreadable."""
        try:
            return now - max([os.path.getmtime(path)] + [
                os.path.getmtime(os.path.join(path, f)) for f in os.listdir(path)])
        except OSError:
            return None

    for name in names:
        path = os.path.join(base, name)
        # Old age takes anything, whatever its key. The group check below only
        # ever reached "pgid-" directories, so a pane-keyed one was kept for
        # ever against the chance that somebody comes back to that window — and
        # they do come back, but not after a fortnight, while the databases are
        # hundreds of megabytes each and one per window.
        if path != own:
            idle = untouched(path)
            if idle is not None and idle > SWEEP_OLD_S:
                shutil.rmtree(path, ignore_errors=True)
                continue
        if not name.startswith("pgid-"):
            continue
        if path == own:
            continue
        try:
            group = int(name[len("pgid-"):])
        except ValueError:
            continue
        try:
            os.killpg(group, 0)
            continue  # someone is still in it
        except ProcessLookupError:
            pass
        except OSError:
            continue  # exists, but not ours to signal
        try:
            newest = max([os.path.getmtime(path)] + [
                os.path.getmtime(os.path.join(path, f)) for f in os.listdir(path)])
        except OSError:
            continue
        if now - newest < SWEEP_AFTER_S:
            continue
        shutil.rmtree(path, ignore_errors=True)


def _repair_rollout_paths(directory, canonical):
    """Replace dead session paths in this window's index with shared files.

    An old database seed indexed the shared sessions while CODEX_HOME pointed
    at a temporary probe. The files survived in ~/.codex/sessions, but its
    rollout_path rows still name the deleted probe, so Codex cannot resume by
    ID. Repair only rows whose old path is gone and whose exact session file
    exists under the canonical shared directory.
    """
    import sqlite3
    from pathlib import Path

    database = Path(directory) / "state_5.sqlite"
    sessions = Path(canonical) / "sessions"
    if not database.is_file() or not sessions.is_dir():
        return
    try:
        with sqlite3.connect(database, timeout=2) as connection:
            fixes = []
            for session_id, old in connection.execute(
                "SELECT id, rollout_path FROM threads WHERE rollout_path IS NOT NULL"
            ):
                if not old or os.path.isfile(old):
                    continue
                parts = Path(old).parts
                if "sessions" not in parts:
                    continue
                suffix = parts[max(i for i, part in enumerate(parts)
                                   if part == "sessions") + 1:]
                if (len(suffix) != 4
                        or not re.fullmatch(r"\d{4}", suffix[0])
                        or not re.fullmatch(r"\d{2}", suffix[1])
                        or not re.fullmatch(r"\d{2}", suffix[2])):
                    continue
                if not re.fullmatch(
                    r"rollout-[^/]*-" + re.escape(session_id) + r"\.jsonl", suffix[3]
                ):
                    continue
                current = sessions.joinpath(*suffix)
                if current.is_file():
                    fixes.append((str(current), session_id, old))
            if fixes:
                connection.executemany(
                    "UPDATE threads SET rollout_path = ? WHERE id = ? AND rollout_path = ?",
                    fixes,
                )
    except (OSError, sqlite3.Error) as exc:
        from ..out import warn
        warn("could not repair Codex session paths (%s)" % exc)


def _unlink_shared_databases(home, canonical):
    """Take the links to the machine-wide databases out of this profile.

    The harness puts them there itself, every start: whatever CODEX_HOME says,
    each database in it becomes a link back into the canonical directory. With
    CODEX_SQLITE_HOME set the real files go elsewhere and the links are merely
    stale — except they are not merely anything. One of them is the telemetry
    database, and a start is REFUSED while another process holds a write lock
    on it. On a machine with seven of these open, a box would sit at "model:
    loading" until something let go, which is to say it would not start at all
    — while a plain run on the host, already holding the file, started fine.
    That reads as "the box is broken".

    Only links, and only inside a profile: a real database here is someone's
    data, and the canonical directory is not ours to prune.
    """
    import glob as globmodule

    if not home:
        return
    home = os.path.realpath(os.path.expanduser(home))
    if home == os.path.realpath(canonical):
        return
    for path in globmodule.glob(os.path.join(home, "*.sqlite*")):
        if os.path.islink(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _seed_databases(own, canonical):
    """Start a window's databases from the ones already built.

    Left to itself a new set is filled from every rollout on disk — fourteen
    thousand files here — and the window sits on "Waiting for startup" for
    minutes while it happens, once per window. So it starts from a copy
    instead, and only what is missing gets read.

    The canonical directory is the source: now that every window has its own,
    nothing writes there any more, which makes it the one copy that is never
    half-written. A clone on APFS costs no space and no time — 600 MB in five
    milliseconds, measured — and where the filesystem cannot clone, this is a
    plain copy, still cheaper than reading every session.

    Only the databases themselves: -wal and -shm belong to whoever had the file
    open, and a journal that does not match its database is exactly how a fresh
    start turns into "database disk image is malformed".
    """
    import glob as globmodule
    import shutil
    import subprocess

    for source in sorted(globmodule.glob(os.path.join(canonical, "*.sqlite"))):
        target = os.path.join(own, os.path.basename(source))
        if os.path.exists(target):
            continue
        try:
            if subprocess.run(["cp", "-c", source, target],
                              capture_output=True).returncode:
                shutil.copy2(source, target)
        except OSError:
            return  # an empty set still works, it is only slower


# The tables that hold a conversation, and the column that ties a row to one.
# state_5 holds the index a resume looks up. thread_history holds the body:
# this harness moved the turns of a conversation into SQLite and now writes
# almost none of them to the rollout file.
THREAD_TABLES = (
    ("state_5.sqlite", (("threads", "id"),)),
    ("thread_history_1.sqlite", (("thread_items", "thread_id"),
                                 ("thread_turns", "thread_id"))),
)

# One publisher at a time. Every one of them is a broker on this host, so a
# file lock is enough — which is the whole reason this works at all.
SHARE_LOCK = os.path.join(SQLITE_BASE, "share.lock")
SHARE_WAIT_S = 2


def _share_threads(own, canonical):
    """Make a conversation written in one window readable from every window.

    Each window gets its own databases because this harness cannot share one:
    a SQLite file written from a container and from the host at the same time
    tears, because the lock does not cross the boundary. Nothing, though, ever
    carried a window's conversations back out again, so a thread existed only
    for the window that created it.

    Measured here: the shared index knew 13,777 threads, the nine window copies
    knew between 13,780 and 13,807, and one thread the person asked for lived in
    three of the nine. Every other window answered "no rollout found for thread
    id", which reads as lost work and is a thread sitting in the window next to
    it.

    The shared set is the meeting point, and no container ever mounts it: these
    databases are not in BOX_SHARED, and a link to them is taken out of the
    profile at every start. So it has exactly one writer at a time, on this
    host, where the lock does work.

    Rows are only ever added. A thread the other side already holds is left
    exactly as that side holds it.
    """
    handle = _share_lock()
    if handle is None:
        return  # another window is at it. A start must not wait on this.
    try:
        for name, tables in THREAD_TABLES:
            mine = os.path.join(own, name)
            theirs = os.path.join(canonical, name)
            if not os.path.isfile(mine) or not os.path.isfile(theirs):
                continue
            try:
                _share_one(mine, theirs, tables)
            except (OSError, sqlite3.Error) as exc:
                from ..out import warn
                warn("could not share Codex threads through %s (%s)"
                     % (os.path.basename(theirs), exc))
    finally:
        handle.close()


def _share_lock():
    """The lock, or None when somebody else holds it.

    Never a blocking wait: this runs before every start, and a window that
    cannot share its threads this second still has to open.
    """
    import fcntl
    import time

    try:
        os.makedirs(os.path.dirname(SHARE_LOCK), mode=0o700, exist_ok=True)
        handle = open(SHARE_LOCK, "a")
    except OSError:
        return None
    for _ in range(int(SHARE_WAIT_S * 10)):
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            time.sleep(0.1)
    handle.close()
    return None


def _share_one(mine, theirs, tables):
    """Share one pair of databases, and say which way the rows may travel."""
    ours = shared = None
    connection = sqlite3.connect(mine, timeout=10)
    try:
        connection.execute("ATTACH DATABASE ? AS shared", (theirs,))
        ours = _schema_version(connection, "main")
        shared = _schema_version(connection, "shared")
        if ours is None or shared is None:
            return  # an empty file somebody left behind, not a database of ours
        # Down first, always: this copy is either on the same migration as the
        # shared one or on a later one, and a later schema has the columns of
        # the earlier one. So the rows fit.
        for table, key in tables:
            _copy_new_threads(connection, table, key, "shared", "main")
        # Up only on the same migration. Writing into an earlier schema would
        # mean dropping the columns it does not have yet, and the next window to
        # read the row would find the fields it needs missing.
        if ours == shared:
            for table, key in tables:
                _copy_new_threads(connection, table, key, "main", "shared")
        connection.commit()
    finally:
        connection.close()  # this detaches the other database too
    if ours is not None and shared is not None and ours > shared:
        _publish(mine, theirs)


def _publish(mine, theirs):
    """Put this window's copy where the shared one was, schema and all.

    The harness does not open the shared set any more, so nothing migrates it:
    measured here, the windows were on migration 59 of the index and 8 of the
    history while the shared copy sat on 55 and 6, four and two behind. A merge
    into it had nowhere to put the newer columns, so the shared copy could never
    learn a new thread again — which is the state this whole machine was in.

    The copy already holds every thread the shared one held: it was merged down
    first, inside the same lock. So handing the file over loses nothing and
    brings the schema forward in the one way that is certainly right, which is
    the harness's own migration of its own database.

    Copied through SQLite rather than cloned: a clone of a file another process
    is writing can tear, and this file is the one every later window starts
    from. 634 MB takes 1.1 s, measured, and this happens once per migration.
    """
    staging = theirs + ".sharing"
    try:
        source = sqlite3.connect("file:%s?mode=ro" % mine, uri=True)
        try:
            target = sqlite3.connect(staging)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        os.replace(staging, theirs)
    except (OSError, sqlite3.Error):
        for leftover in (staging, staging + "-wal", staging + "-shm"):
            try:
                os.unlink(leftover)
            except OSError:
                pass
        raise
    # The journals of the file that was here belong to a database that is gone.
    # Leaving one beside the new file is how a fresh start reads "database disk
    # image is malformed".
    for extra in ("-wal", "-shm"):
        try:
            os.unlink(theirs + extra)
        except OSError:
            pass


def _schema_version(connection, schema):
    """The migration this database last applied, or None when it has none."""
    try:
        row = connection.execute(
            "SELECT max(version) FROM %s._sqlx_migrations" % schema).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] is not None else None


def _shared_columns(connection, table):
    """The columns both copies of a table have, in the order the first holds them."""
    mine = [row[1] for row in connection.execute("PRAGMA main.table_info(%s)" % table)]
    theirs = {row[1] for row in connection.execute("PRAGMA shared.table_info(%s)" % table)}
    return [name for name in mine if name in theirs]


def _copy_new_threads(connection, table, key, source, target):
    """Copy the rows of the threads the target has never seen.

    By thread, not by row. Half a conversation is worse than none of it, and a
    row-level merge of a table the harness writes as well would have to decide
    which copy of a row is the current one. A thread the target does not have at
    all asks no such question.
    """
    columns = _shared_columns(connection, table)
    if not columns:
        return  # one side does not have this table: an empty file, or an older set
    names = ", ".join('"%s"' % column for column in columns)
    connection.execute(
        'INSERT OR IGNORE INTO %s."%s" (%s) SELECT %s FROM %s."%s" '
        'WHERE "%s" NOT IN (SELECT "%s" FROM %s."%s")'
        % (target, table, names, names, source, table, key, key, target, table))
