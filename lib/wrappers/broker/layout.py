"""Laying a profile out: links back to the shared home, or a mirror of it."""

import os
import shutil
from pathlib import Path

from .out import warn
from .profile import private_paths, shared_names, _private_tree
from .tree import _drop_dead_links, _entries, _link


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
        # A lock that is not shared is not a lock. These directories exist so
        # two processes do not refresh the same token at once; one per profile
        # means several accounts refresh together, one wins, and the rest are
        # left holding a refresh token the server has just revoked. They hold
        # no data — only empty lock files — so replacing a private one is safe
        # where replacing anything else would not be.
        if name in getattr(provider, "LOCKED_TOGETHER", ()) and os.path.isdir(src):
            if os.path.isdir(dst) and not os.path.islink(dst):
                try:
                    shutil.rmtree(dst)
                except OSError as exc:
                    warn("could not share the lock directory %s: %s" % (name, exc))

        if os.path.exists(src) and not os.path.lexists(dst):
            try:
                # Some files a harness refuses to open through a symlink, as a
                # guard against being handed someone else's: it reports "too
                # many levels of symbolic links" for a single one. Give those a
                # second NAME instead — one file either way, so a token
                # refreshed under one account is refreshed under all at once.
                if name in getattr(provider, "HARD_LINKED", ()) and os.path.isfile(src):
                    os.link(src, dst)
                else:
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


def mirror(provider, path, isolate_mcp=None):
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
    _mirror_level(provider.CANONICAL_HOME, path,
                  _private_tree(private_paths(provider, isolate_mcp)))


def _mirror_level(canonical, path, tree):
    """One directory of the mirror, then the private directories under it."""
    # A directory we are about to split may already be a link to the canonical
    # one — that is what it was before this run asked for the split. Dropping it
    # turns it into a real directory whose contents are linked one by one, which
    # is the whole point. Only a link INTO the directory being mirrored goes: a
    # real directory is left alone, and so is a link the user aimed elsewhere.
    if os.path.islink(path):
        if os.path.realpath(path) != os.path.realpath(canonical):
            return  # someone pointed this somewhere deliberately
        try:
            os.unlink(path)
        except OSError as exc:
            warn("cannot split %s: %s" % (path, exc))
            return
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as exc:
        warn("cannot create %s: %s" % (path, exc))
        return
    for name in _entries(canonical):
        if name in tree:
            continue
        _link(os.path.join(canonical, name), os.path.join(path, name))
    _drop_dead_links(path, canonical, tree)
    for name, below in tree.items():
        # A leaf is the private file itself: nothing to descend into, and
        # write_auth is about to put the real credentials there.
        if not below:
            continue
        _mirror_level(os.path.join(canonical, name),
                      os.path.join(path, name), below)


def promote(provider, path):
    """Move a file the harness invented in a profile into the shared home.

    link_shared can only point at what already exists: it walks the names that
    are shared, finds them in the canonical home, and links them. A harness
    that invents a NEW one defeats that. codex added state_5.sqlite, then
    thread_history_1.sqlite, at a moment when the canonical home had nothing by
    those names — nothing to link to, so it wrote its own inside whichever
    profile ran first. From then on the name was taken, link_shared left it
    alone as it must, and the accounts drifted apart in silence: one profile
    ended up holding the only copy of a 13,722-thread history while the others
    wrote into an empty database of their own.

    So a real file whose name is meant to be shared, and which the canonical
    home does not have, is moved there and left behind as a link. Nothing can
    be lost doing it: there is no file on the other side to overwrite. The move
    is a rename within one volume, so anything holding the file open keeps
    writing to the same inode.

    A name the canonical home DOES have is a different matter — one of the two
    copies would have to lose — and that stays a deliberate act:
    `broker <provider> refresh --share`.
    """
    canonical = provider.CANONICAL_HOME
    if os.path.realpath(path) == os.path.realpath(canonical):
        return []

    moved = []
    for pattern in getattr(provider, "SHARED_GLOBS", ()):
        for src in sorted(Path(path).glob(pattern)):
            if src.is_symlink() or not src.is_file():
                continue
            dst = os.path.join(canonical, src.name)
            if os.path.lexists(dst):
                continue
            try:
                os.replace(str(src), dst)
                # sqlite keeps -wal/-shm beside the real file; leaving them
                # behind would strand a checkpoint that has not been folded in.
                for side in ("-wal", "-shm"):
                    if os.path.exists(str(src) + side):
                        os.replace(str(src) + side, dst + side)
                os.symlink(dst, str(src))
                moved.append(src.name)
            except OSError as exc:
                # A different volume, or no permission: leave it where it is
                # rather than half-move it.
                warn("could not share %s: %s" % (src.name, exc))
    return moved


def prepare(provider, path):
    """Lay out a profile the way this provider needs it."""
    if getattr(provider, "MIRROR_HOME", False):
        return mirror(provider, path)
    # Fresh containers have no canonical home: do not rely on a host login or
    # an entrypoint creating it before promotion and directory enumeration.
    os.makedirs(provider.CANONICAL_HOME, mode=0o700, exist_ok=True)
    # Before linking: anything the harness invented in here, which belongs to
    # everyone, goes to the shared home first — otherwise the link below has
    # nothing to point at and the file stays private for good.
    promote(provider, path)
    return link_shared(provider, path)
