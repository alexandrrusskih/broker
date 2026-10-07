"""Copies a box keeps to itself, so that writing inside changes nothing outside."""

import os
import shutil
import subprocess

from .. import config
from ..out import warn
from .paths import _bind, expand
from .sync import _StoreLock


def _clone(source, destination):
    """Copy that costs nothing until something is written. Files or trees.

    These files are databases, and one of them is nearly four gigabytes; copying
    that on every start would be absurd. APFS clones instead: the copy shares
    the same blocks until one side changes them, which is exactly the shape of
    this — a box reads almost all of it and writes a little. `cp -c` asks for
    that and falls back on its own when the filesystem cannot.
    """
    if os.path.isdir(destination):
        shutil.rmtree(destination)
    elif os.path.exists(destination):
        os.remove(destination)
    try:
        subprocess.run(["cp", "-c", "-R", source, destination], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        if os.path.isdir(source):
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)


def blank(provider, name, mounted):
    """One empty copy per named file, writable, never seeded from here."""
    args = []
    # Files a box gets EMPTY and keeps to itself: it may write them, and what
    # it writes stays inside. Not a secret it must not see (that is above, and
    # is read-only) — a file this harness rewrites WHOLE, dropping whatever it
    # did not put there itself.
    #
    # agy's MCP tokens are the case this exists for. Mounted straight through,
    # a box emptied that file and two logins granted an hour earlier were gone,
    # reported afterwards as "Unauthorized [Auth Needed]" — which reads as an
    # expired token rather than as a file cleared by a program next door. Four
    # times in one afternoon.
    #
    # Read-only would stop the damage and cost more than it saves: the harness
    # then fails on a write it expects to succeed, rather than carrying on
    # without the servers that need a login. So: its own copy, writable,
    # starting out empty, never seeded from here. A box that wants those
    # servers logs in for itself; a box that does not is no worse off than one
    # that never had the file.
    for entry, seed in getattr(provider, "BOX_BLANK", ()):
        host = expand(entry)
        if not any(host.startswith(m + os.sep) for m in mounted):
            continue
        own = os.path.join(config.CONFIG_DIR, "box", "blank", name.replace("/", "_"),
                           host.lstrip(os.sep).replace(os.sep, "_"))
        try:
            os.makedirs(os.path.dirname(own), mode=0o700, exist_ok=True)
            if not os.path.exists(own):
                with open(own, "w") as handle:
                    handle.write(seed)
                os.chmod(own, 0o600)
        except OSError as exc:
            warn("could not give the box a blank %s (%s)" % (entry, exc))
            continue
        args += _bind(own, host)
    return args


def readonly(provider, mounted):
    """The real files, mounted so that the worst a box can do is fail to write."""
    args = []
    # Files a box may READ but must not touch. Not the same as a secret it may
    # not see at all: a harness inside needs these to work, and needs them to
    # be the real ones. agy's MCP tokens are the case this exists for — started
    # in a box it rewrote that file EMPTY, and two logins granted an hour
    # earlier were gone, reported afterwards as "Unauthorized [Auth Needed]",
    # which reads as an expired token rather than as a file destroyed by a
    # program on the same machine. Mounted read-only, the worst it can do is
    # fail to write.
    for entry in getattr(provider, "BOX_READONLY", ()):
        host = expand(entry)
        if os.path.exists(host) and any(host.startswith(m + os.sep) for m in mounted):
            args += _bind(host, host, "ro")
    return args


def private(provider, profile, env, name):
    """One clone per real file, mounted at every name the harness may open it by."""
    args = []
    # Files a box must not share with the host, however they got there: cloned
    # in, so writing them inside changes nothing outside. One clone per real
    # file — every profile's copy is a symlink to the same one — mounted at each
    # name the harness might open it by.
    private = getattr(provider, "BOX_PRIVATE", ())
    home_env_now = getattr(provider, "HOME_ENV", None)
    in_use = (env or {}).get(home_env_now) if home_env_now and home_env_now != "HOME" else None
    if private:
        import glob as globmodule

        clones = {}
        # Nobody may be registering a session out here while these are copied.
        store_lock = _StoreLock(provider)
        store_lock.__enter__()
        for base in [expand(getattr(provider, "CANONICAL_HOME", "~"))] + ([expand(in_use)] if in_use else []):
            for pattern in private:
                for host in sorted(globmodule.glob(os.path.join(base, pattern))):
                    real = os.path.realpath(host)
                    copy = clones.get(real)
                    if copy is None:
                        copy = os.path.join(config.CONFIG_DIR, "box", "private",
                                            name.replace("/", "_"), provider.NAME,
                                            os.path.basename(real))
                        try:
                            os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
                            # Fresh every start: cloning costs nothing, and a
                            # stale copy is worse than none.
                            _clone(real, copy)
                        except OSError as exc:
                            warn("could not give the box its own %s (%s)" % (os.path.basename(real), exc))
                            continue
                        clones[real] = copy
                    args += _bind(copy, host)
                    # sqlite keeps its write-ahead log and shared-memory file
                    # BESIDE the database, and those are part of its state: the
                    # newest pages live in -wal until a checkpoint folds them
                    # in. Cloning the database alone leaves them coming from
                    # the directory mount underneath — so the box wrote its
                    # pages into its own copy and its journal into everyone's,
                    # and both tore. Measured as "wrong # of entries in index"
                    # on the host while a box was running.
                    #
                    # Mounted even when the host has no such file yet, or
                    # sqlite would create one in the shared directory the
                    # moment it opens the database.
                    for side in ("-wal", "-shm"):
                        beside = copy + side
                        try:
                            if os.path.exists(real + side):
                                _clone(real + side, beside)
                            elif not os.path.exists(beside):
                                open(beside, "a").close()
                        except OSError as exc:
                            warn("could not give the box its own %s (%s)"
                                 % (os.path.basename(real) + side, exc))
                            continue
                        args += _bind(beside, host + side)

        # Copied; whoever wants to register a session may go ahead.
        store_lock.__exit__()
    return args
