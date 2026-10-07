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
# On the big disk, not in the home: a filled index is some 600 MB, and there
# are a dozen windows.
SQLITE_ENV = "CODEX_SQLITE_HOME"
SQLITE_BASE = "/Volumes/hdd/broker-box/codex-db"


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

    base = SQLITE_BASE if os.path.isdir(os.path.dirname(SQLITE_BASE)) else \
        os.path.expanduser("~/.cache/broker/codex-db")
    own = os.path.join(base, window_key())
    fresh = not os.path.isdir(own)
    try:
        os.makedirs(own, mode=0o700, exist_ok=True)
    except OSError:
        return  # the shared ones are worse, but they are better than no start
    if fresh:
        _seed_databases(own, canonical)
    env[SQLITE_ENV] = own
    _repair_rollout_paths(own, canonical)


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
