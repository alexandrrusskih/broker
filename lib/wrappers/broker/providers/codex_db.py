"""A session on disk: the error it ended on, and the databases that index it.

One set of databases per window, because this harness cannot share them.

The canonical home is passed in rather than imported: the provider module
declares it, and the provider module imports this one.
"""

import os
import re


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
