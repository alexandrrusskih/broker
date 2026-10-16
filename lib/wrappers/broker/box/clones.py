"""Copies a box keeps to itself, so that writing inside changes nothing outside."""

import json
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


# Which servers may have their login held in more than one place at once.
#
# This is not a preference. A refresh token that is SINGLE-USE cannot be
# copied: the first holder to spend it gets a successor, the second presents a
# token the server has already consumed, and a server that detects that revokes
# the whole family — so sharing a login would log everybody out of it instead
# of carrying it around.
#
#   ntk     safe. crates/ntk-api/src/mcp_oauth.rs does not consume the token it
#           was shown. Rotation with reuse-detection was tried there on
#           2026-09-23 and rolled back four hours later: it fired eight times
#           and threw working people out, with no theft behind any of it. The
#           reason given is this exact situation — "параллельные процессы с
#           одной копией".
#   joppa   NOT safe. src/mcp_oauth.rs inserts the presented token into
#           oauth_consumed and, when that insert finds it already there,
#           inserts the family into oauth_revoked: "Replaying a refresh token
#           revokes the entire family, including its successor."
#
# So the list is an allowlist and the default is to leave a login where it was
# made. Adding a server means reading its token endpoint first, not guessing.
# The real answer for the rest is for the broker to hold the refresh token
# itself and hand out only access tokens, which is one holder by construction.
SHAREABLE = ("https://ntk.otion.us/",)


def _shareable(server):
    """Whether this server's login may exist in two places at once."""
    return isinstance(server, str) and server.startswith(SHAREABLE)


def _entries(path):
    """The file as a store of named entries, or None if it is not one.

    Deliberately narrow. A clone may be a four-gigabyte database, and nothing
    here should try to be clever about one: only a small JSON object whose
    every value is an object counts, which is the shape of an MCP token store
    — one entry per server URL.
    """
    if not path.endswith(".json"):
        return None
    try:
        if os.path.getsize(path) > 1024 * 1024:
            return None
        with open(path) as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or not loaded:
        return None
    if any(not isinstance(value, dict) for value in loaded.values()):
        return None
    return loaded


def _later(mine, theirs):
    """Whether `mine` holds the fresher token of two entries for one server."""
    def when(entry):
        stamp = (entry.get("token") or {}).get("expiry")
        return stamp if isinstance(stamp, str) else ""

    # String order is time order for ISO-8601 instants with the same shape, and
    # when it is not — a missing or odd stamp — the answer is "no", which keeps
    # whatever is already on the host.
    return when(mine) > when(theirs)


def _carry_back(real, name):
    """Fold what boxes authorized into the host's own store.

    A box gets a CLONE of this file, because the harness rewrites it whole and
    a shared one let a box next door empty everybody's logins. The cost was
    the other direction: a login made inside a box died with the box, and a
    person who authorized a server in the box they were working in had to do
    it again outside, and again in the next box. Said plainly, authorization
    was not transparent, and that is the complaint this answers.

    Nothing is replaced. Entries the host does not have are added; an entry
    both sides have is taken from whichever holds the later expiry. So a login
    made anywhere reaches the host, and from there every box's next clone,
    while a token the host refreshed is never pushed backwards by a box's
    older copy. A file that is not a store of entries is left alone entirely.
    """
    host = _entries(real)
    if host is None:
        return
    root = os.path.join(config.CONFIG_DIR, "box", "private")
    merged = dict(host)
    changed = []
    for box in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        clone = os.path.join(root, box, name, os.path.basename(real))
        theirs = _entries(clone) if os.path.exists(clone) else None
        if not theirs:
            continue
        for server, entry in theirs.items():
            if not _shareable(server):
                continue  # its login stays where it was made; see SHAREABLE
            if server not in merged or _later(entry, merged[server]):
                merged[server] = entry
                changed.append("%s from %s" % (server, box))
    if not changed:
        return
    try:
        config.write_json(real, merged, prefix=".tokens-")
    except OSError as exc:
        warn("could not carry a box's logins back into %s (%s)"
             % (os.path.basename(real), exc))
        return
    warn("carried %d login(s) back into %s: %s"
         % (len(changed), os.path.basename(real), ", ".join(changed)))


def _mirror_out(real, name, mine):
    """Put the host's store into the clone of every box already running.

    The other half of transparency. A clone is taken when a box starts, so a
    login granted out here reached a box that was ALREADY running only after
    that box was restarted — which is most of what "authorization does not
    arrive" meant. There is no watcher and no daemon: every broker run pushes
    the current file outwards, and a broker run is what starting any harness
    or any bridge already is, so a box sees a new login within moments rather
    than at its next start.

    Written IN PLACE, not replaced. A bind mount holds the inode it was given:
    writing a new file and renaming it over the clone — which is what the
    atomic writer does, correctly, everywhere else — would leave every running
    box bound to the old inode and blind to this. The file is a few hundred
    bytes and goes out in one write; a box reading at that instant is the
    accepted cost of not breaking its mount.

    The box whose clone was just made is skipped: it has the file already.
    """
    host = _entries(real)
    if host is None:
        return
    offer = {server: entry for server, entry in host.items() if _shareable(server)}
    if not offer:
        return
    root = os.path.join(config.CONFIG_DIR, "box", "private")
    for box in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        clone = os.path.join(root, box, name, os.path.basename(real))
        if clone == mine or not os.path.exists(clone):
            continue
        theirs = _entries(clone)
        if theirs is None:
            continue
        # Merged INTO the clone, never over it. A box may hold a login for a
        # server that is not shareable — its own, made inside — and writing the
        # host's file wholesale would delete exactly the thing a box cannot get
        # back.
        wanted = dict(theirs)
        for server, entry in offer.items():
            if server not in wanted or _later(entry, wanted[server]):
                wanted[server] = entry
        if wanted == theirs:
            continue  # nothing new; a write would only risk a torn read
        try:
            with open(clone, "w") as handle:
                handle.write(json.dumps(wanted, indent=2, sort_keys=True))
        except OSError as exc:
            warn("could not mirror %s into the %s box (%s)"
                 % (os.path.basename(real), box, exc))


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
    # be the real ones.
    #
    # Each provider names what the HOST runs or reads as instruction from its
    # own directory — hooks, skills, agents, commands, plugin code, prompt
    # files, and for codex the binaries the host's own codex starts. That
    # directory travels WRITABLE, because the harness keeps its state there, so
    # without this list a box could rewrite one and the harness out here would
    # run it at its next start. Prompt files are the quieter half of the same
    # hole: a line added to one reaches every chat on this machine.
    #
    # Cache and state are deliberately NOT in those lists. A box that cannot
    # write its own cache is a box that does not work. agy's MCP tokens are the case this exists for — started
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
                            # Before the clone is replaced, whatever any box
                            # authorized goes home: the clone about to be
                            # overwritten is this box's own from last time,
                            # and the others belong to boxes that have since
                            # exited. This is what makes a login transparent
                            # in both directions.
                            _carry_back(real, provider.NAME)
                            # Fresh every start: cloning costs nothing, and a
                            # stale copy is worse than none.
                            _clone(real, copy)
                            # And outwards, to the boxes already running, so a
                            # login does not wait for their next start.
                            _mirror_out(real, provider.NAME, copy)
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
                    #
                    # Only for a database, though. A private file that is not
                    # one — a login, a small JSON — got an auth.json-wal and an
                    # auth.json-shm invented beside it and mounted in, which is
                    # noise at best and a puzzle for whoever reads the mounts.
                    if not real.endswith((".db", ".sqlite", ".sqlite3")):
                        continue
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
