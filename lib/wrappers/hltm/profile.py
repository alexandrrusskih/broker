"""Per-account profiles: their own auth file, shared everything else."""

import json
import os
import tempfile
from pathlib import Path

from .out import warn


def profile_dir(provider, account):
    """Where this account's config dir lives. An explicit one always wins.

    Every account gets its own directory, including whichever one you started
    with: the canonical home stays put as the shared original the profiles link
    back to, so no account inherits it by being special.
    """
    env_name = getattr(provider, "PROFILE_ENV", provider.HOME_ENV)
    explicit = env_name and os.environ.get(env_name)
    if explicit:
        return os.path.expanduser(explicit)
    base = getattr(provider, "PROFILE_BASE", None) or provider.CANONICAL_HOME
    return "%s-%s" % (base, account)


def shared_names(provider):
    """Everything shared with the canonical home: fixed names plus glob matches.

    A mirroring provider has no such list — everything except the credentials is
    shared — so this is empty for one, and callers use shared_entries instead.
    """
    names = list(getattr(provider, "SHARED", ()))
    for pattern in getattr(provider, "SHARED_GLOBS", ()):
        names += [p.name for p in Path(provider.CANONICAL_HOME).glob(pattern)]
    return names


def shared_entries(provider, path):
    """(canonical source, place in this profile) for everything meant to be shared.

    The two profile layouts answer this differently — a list of names for codex,
    every entry except the path down to the token for agy — and callers that only
    want "what should be a link here" should not have to know which is which.
    """
    if not getattr(provider, "MIRROR_HOME", False):
        return [
            (os.path.join(provider.CANONICAL_HOME, name), os.path.join(path, name))
            for name in shared_names(provider)
        ]

    pairs = []
    canonical, dst = provider.CANONICAL_HOME, path
    parts = provider.AUTH_NAME.split(os.sep)
    for depth, private in enumerate(parts):
        for name in _entries(canonical):
            if name == private:
                continue
            pairs.append((os.path.join(canonical, name), os.path.join(dst, name)))
        if depth == len(parts) - 1:
            break
        canonical = os.path.join(canonical, private)
        dst = os.path.join(dst, private)
    return pairs


def profile_root(provider):
    """The directory profiles live beside, and the prefix their names carry."""
    base = getattr(provider, "PROFILE_BASE", None) or provider.CANONICAL_HOME
    return os.path.dirname(base), os.path.basename(base) + "-"


def link_shared(provider, path):
    """Point the shared parts of a profile back at the canonical home.

    Idempotent: it creates missing links, and drops the ones it made for names
    that are no longer shared — so a profile follows the current list instead of
    whatever it was created with. A real file, or a link the user pointed
    elsewhere, is left alone.
    """
    canonical = provider.CANONICAL_HOME
    if os.path.realpath(path) == os.path.realpath(canonical):
        return

    shared = shared_names(provider)
    for name in shared:
        src = os.path.join(canonical, name)
        dst = os.path.join(path, name)
        if os.path.exists(src) and not os.path.lexists(dst):
            try:
                os.symlink(src, dst)
            except OSError as exc:
                warn("could not link %s: %s" % (name, exc))

    try:
        entries = os.listdir(path)
    except OSError:
        return
    for name in entries:
        if name in shared:
            continue
        dst = os.path.join(path, name)
        if not os.path.islink(dst):
            continue
        if os.path.realpath(dst) == os.path.realpath(os.path.join(canonical, name)):
            try:
                os.unlink(dst)
            except OSError as exc:
                warn("could not unlink %s: %s" % (name, exc))


def mirror(provider, path):
    """Make `path` a copy of the canonical home in which one file is private.

    codex hands its profile a dedicated variable, so a profile is a small
    directory of links. agy has no such variable — the only way to give an
    account its own credentials is to hand the process a different HOME, and a
    different HOME must still look exactly like the real one or the harness
    loses its settings, its history and its trusted workspaces.

    So every entry is linked back to the real home, except the directories on
    the way down to the credentials file: those are made real and mirrored the
    same way, one level deeper. The result is a home that differs from yours in
    exactly one file.
    """
    parts = provider.AUTH_NAME.split(os.sep)
    canonical = provider.CANONICAL_HOME
    for depth, private in enumerate(parts):
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError as exc:
            warn("cannot create %s: %s" % (path, exc))
            return
        for name in _entries(canonical):
            if name == private:
                continue
            _link(os.path.join(canonical, name), os.path.join(path, name))
        _drop_dead_links(path, canonical, private)
        # `private` is the credentials file itself on the last turn: nothing to
        # descend into, and write_auth is about to put the real one there.
        if depth == len(parts) - 1:
            return
        canonical = os.path.join(canonical, private)
        path = os.path.join(path, private)


def _drop_dead_links(path, canonical, private):
    """Remove links this mirror made to things that no longer exist.

    A mirror is built from whatever the real home held that day, so files deleted
    since leave dangling links behind — dozens of them in a profile that has been
    around a while. Only links pointing into the mirrored directory are touched:
    a real file, or a link the user aimed elsewhere, is left alone.
    """
    for name in _entries(path):
        if name == private:
            continue
        dst = os.path.join(path, name)
        if not os.path.islink(dst) or os.path.exists(dst):
            continue
        if os.path.dirname(os.readlink(dst)) != canonical.rstrip(os.sep):
            continue
        try:
            os.unlink(dst)
        except OSError as exc:
            warn("could not drop the dead link %s: %s" % (dst, exc))


def _entries(directory):
    try:
        return os.listdir(directory)
    except OSError:
        return []


def _link(src, dst):
    """Link one shared entry, leaving anything already there alone."""
    if not os.path.lexists(dst):
        try:
            os.symlink(src, dst)
        except OSError as exc:
            warn("could not link %s: %s" % (dst, exc))
        return
    # A link that points at the wrong place is worse than no link: it silently
    # feeds the harness someone else's state. Repoint ours; leave real files and
    # links the user aimed elsewhere untouched.
    if os.path.islink(dst) and os.path.realpath(dst) != os.path.realpath(src):
        parent = os.path.dirname(os.path.realpath(dst))
        if os.path.realpath(parent) == os.path.realpath(os.path.dirname(src)):
            try:
                os.unlink(dst)
                os.symlink(src, dst)
            except OSError as exc:
                warn("could not relink %s: %s" % (dst, exc))


def prepare(provider, path):
    """Lay out a profile the way this provider needs it."""
    if getattr(provider, "MIRROR_HOME", False):
        return mirror(provider, path)
    return link_shared(provider, path)


def write_auth(provider, path, auth):
    """Install the auth file atomically and 0600 — a half-written token file
    would otherwise replace a working one on a dropped connection."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    prepare(provider, path)
    # AUTH_NAME may be a path (agy keeps its token three levels down), so the
    # temp file has to be written beside the destination, not at the top.
    target = os.path.join(path, provider.AUTH_NAME)
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".auth-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(auth, fh)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cache_dir():
    """Where cached credentials go.

    Beside the config normally — but that whole tree is mounted READ-ONLY inside
    medulla's container, and a cache that cannot be written must not look like a
    failure. Fall back to the temp dir, which every container has.
    """
    preferred = os.path.expanduser("~/.config/hltm-broker/cache")
    try:
        os.makedirs(preferred, mode=0o700, exist_ok=True)
        if os.access(preferred, os.W_OK):
            return preferred
    except OSError:
        pass
    fallback = os.path.join(os.environ.get("TMPDIR", "/tmp"), "hltm-broker-cache-%d" % os.getuid())
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    return fallback


CACHE_DIR = os.path.expanduser("~/.config/hltm-broker/cache")


def cache_path(provider, account):
    return os.path.join(_cache_dir(), "%s-%s.json" % (provider.NAME, account))


def write_cache(provider, account, auth):
    """Keep the broker's answer for a provider that has no profile on disk.

    claude takes its credentials through the environment, so there is no auth
    file to fall back on when the broker cannot be reached. This is that
    fallback, and nothing else reads it.
    """
    directory = _cache_dir()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cache-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(auth, fh)
        os.replace(tmp, cache_path(provider, account))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_auth(provider, account):
    """The auth file this profile already holds, if any.

    The broker is the source of truth, but its copy is written here on every
    successful run — so when the broker cannot be reached, this is a real token
    that was valid the last time anyone looked.
    """
    if getattr(provider, "CREDENTIALS", "file") == "env":
        path = cache_path(provider, account)
    else:
        path = os.path.join(profile_dir(provider, account), provider.AUTH_NAME)
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def ensure(provider, rows):
    """Create and re-link a profile for each account the broker knows.

    Pure filesystem work — a fraction of a millisecond next to the probe that
    just ran — so the pick path does it silently: a profile seeded on another
    machine is ready here without a separate step, and a name added to SHARED
    reaches existing profiles on its own.
    """
    for row in rows:
        home = profile_dir(provider, row["account"])
        try:
            os.makedirs(home, mode=0o700, exist_ok=True)
            prepare(provider, home)
        except OSError as exc:
            warn("%s: cannot prepare %s: %s" % (row["account"], home, exc))
