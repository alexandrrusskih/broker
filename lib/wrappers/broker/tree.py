"""The three filesystem moves every profile layout is made of."""

import os

from .out import warn


def _entries(directory):
    try:
        return os.listdir(directory)
    except OSError:
        return []


def _link(src, dst, hard=False):
    """Link one shared entry, leaving anything already there alone.

    `hard` gives the file a second NAME instead of pointing at it. For most
    things a symlink is better — it survives the target being replaced — but
    some a harness refuses to open through one at all, as a guard against
    being handed someone else's file, and reports "too many levels of symbolic
    links" for what is a single link. A hard link is indistinguishable from an
    ordinary file to whoever opens it, and there is still only one file: a
    token refreshed under one account is refreshed under all of them at once,
    with nothing to synchronise and no copy to lose the race.
    """
    if not os.path.lexists(dst):
        try:
            if hard and os.path.isfile(src):
                os.link(src, dst)
            else:
                os.symlink(src, dst)
        except OSError as exc:
            warn("could not link %s: %s" % (dst, exc))
        return
    # Already a name for the same file: nothing to do, and nothing to warn about.
    if hard and os.path.isfile(dst) and not os.path.islink(dst):
        try:
            if os.stat(dst).st_ino == os.stat(src).st_ino:
                return
        except OSError:
            pass
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


def _drop_dead_links(path, canonical, private):
    """Remove links this mirror made to things that no longer exist.

    A mirror is built from whatever the real home held that day, so files deleted
    since leave dangling links behind — dozens of them in a profile that has been
    around a while. Only links pointing into the mirrored directory are touched:
    a real file, or a link the user aimed elsewhere, is left alone.

    `private` is every name this level keeps for itself — one when only the
    credentials are split, more when this run also owns its MCP file.
    """
    keep = set(private) if isinstance(private, (set, dict, list, tuple)) else {private}
    for name in _entries(path):
        if name in keep:
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
