"""Giving every account a profile, and taking a stray file into the shared home."""

import os
import sys

from . import accounts, api, credentials, layout, profile, table
from .out import die, warn


def adopt_shared(provider, home):
    """Replace a profile's own copy of a now-shared file with a link to the
    canonical one, keeping the old copy beside it.

    Profiles created before a name became shared hold a real file, and
    link_shared leaves those alone on purpose — it must never overwrite
    something the user put there. This is the explicit opt-in.
    """
    moved = []
    for src, dst in profile.shared_entries(provider, home):
        name = os.path.relpath(dst, home)
        if not os.path.exists(src) or os.path.islink(dst) or not os.path.exists(dst):
            continue
        keep = dst + ".pre-share"
        try:
            os.replace(dst, keep)
            # The old database's sidecars would sit next to a symlink now, where
            # sqlite never looks (it creates them beside the real file) — leave
            # them beside their own database instead of confusing the next reader.
            for side in ("-wal", "-shm"):
                if os.path.exists(dst + side):
                    os.replace(dst + side, keep + side)
            os.symlink(src, dst)
            moved.append(name)
        except OSError as exc:
            warn("%s: could not adopt %s: %s" % (os.path.basename(home), name, exc))
    return moved


def cmd_refresh(cfg, provider, args):
    """Bring local profiles in line with what the broker holds.

    A bare run already creates and links the profiles it sees; this also installs
    each account's auth file, so every profile works straight away — including
    under a bare harness with an explicit config dir.
    """
    names = api.list_accounts(cfg, provider.NAME, die)
    if not names:
        die("no %s accounts seeded — run '%s auth <name>'" % (provider.NAME, provider.CMD))

    share = "--share" in args
    rows = accounts.probe_all(cfg, provider, names)
    for row in rows:
        home = profile.profile_dir(provider, row["account"])
        existed = os.path.isdir(home)
        try:
            if row["auth"]:
                credentials.write_auth(provider, home, row["auth"])
            else:
                os.makedirs(home, mode=0o700, exist_ok=True)
                layout.prepare(provider, home)
        except OSError as exc:
            warn("%s: cannot prepare %s: %s" % (row["account"], home, exc))
            continue
        note = "" if existed else "(created)"
        if share:
            moved = adopt_shared(provider, home)
            if moved:
                note = "(now shares %s; old copies kept as *.pre-share)" % ", ".join(moved)
        else:
            # A file that should be a link but is not — usually because whatever
            # wrote it did so through a temporary file and a rename, which
            # replaces the link rather than following it. Left unsaid, the two
            # copies drift: that is how one account ended up with a full thread
            # history and the others with an empty one. Not repaired on its own,
            # because repairing means one of the two copies loses.
            drifted = [os.path.relpath(dst, home)
                       for src, dst in profile.shared_entries(provider, home)
                       if os.path.exists(src) and os.path.exists(dst) and not os.path.islink(dst)]
            if drifted:
                note = "(its own %s, not shared — 'refresh --share' to join them)" % ", ".join(drifted)
        warn("%-10s %s %s" % (row["account"], home, note))

    # Profiles left behind by an account that is no longer seeded. Never removed
    # automatically — they still hold that account's history.
    known = {profile.profile_dir(provider, n) for n in names}
    # Where profiles actually live. For codex that is beside ~/.codex; for agy the
    # canonical home is $HOME itself, so deriving it from there walked /Users and
    # reported strangers' home directories as orphan profiles.
    parent, prefix = profile.profile_root(provider)
    for entry in sorted(os.listdir(parent)):
        path = os.path.join(parent, entry)
        if not (entry.startswith(prefix) and os.path.isdir(path)) or path in known:
            continue
        # The account is gone from the broker but its credentials are still here.
        # For agy that is a working refresh token, so leaving it is leaving the
        # account usable by anyone with this disk. The history is what the profile
        # is kept for, not the token.
        stale = os.path.join(path, provider.AUTH_NAME)
        if os.path.isfile(stale):
            try:
                os.remove(stale)
                warn("orphan profile %s — removed its stale credentials" % path)
                continue
            except OSError as exc:
                warn("orphan profile %s — could not remove its credentials: %s" % (path, exc))
                continue
        warn("orphan profile %s — no such account in the broker" % path)

    if not share:
        warn("")
        warn("profiles created earlier keep their own history/state files;")
        warn("run '%s refresh --share' to point them at the shared ones." % provider.CMD)

    print("", file=sys.stderr)
    table.render(cfg, provider, rows)
    return 0
