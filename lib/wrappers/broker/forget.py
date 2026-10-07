"""Deleting an account: in the broker, and on this machine."""

import os
import sys

from . import api, credentials, profile
from .out import die, warn


def cmd_delete_auth(cfg, provider, args):
    """Forget an account in the broker.

    The refresh token goes with it, so this is irreversible: getting the account
    back means a fresh login plus a new seed. The local profile is left alone —
    it holds that account's history.
    """
    names = [a for a in args if not a.startswith("-")]
    if not names:
        die("usage: %s delete-auth <name> [--yes]" % provider.CMD)
    account = names[0]

    known = api.list_accounts(cfg, provider.NAME, die)
    if account not in known:
        die("no such account: %s (have: %s)" % (account, ", ".join(known) or "none"))

    if "--yes" not in args and "-y" not in args:
        if not sys.stdin.isatty():
            die("refusing to delete without a terminal — pass --yes to mean it")
        try:
            answer = input(
                "delete '%s' from the broker? its refresh token is gone for good. "
                "type the name to confirm: " % account
            ).strip()
        except EOFError:
            answer = ""
        if answer != account:
            die("not confirmed — nothing deleted")

    if not api.delete_account(cfg, provider.NAME, account, die):
        warn("%s was not in the broker" % account)
    warn("%s deleted from the broker" % account)

    dropped = drop_credentials(provider, account)
    for path in dropped:
        warn("removed its credentials: %s" % path)

    home = profile.profile_dir(provider, account)
    if getattr(provider, "CREDENTIALS", "file") != "env" and os.path.isdir(home):
        warn("its profile is kept: %s   (it holds this account's history)" % home)
    return 0


def drop_credentials(provider, account):
    """Delete this account's credentials from the machine, keeping its history.

    Revoking an account in the broker used to leave its token sitting in the
    profile — and for agy that is the REAL refresh token, so "deleted" meant
    deleted in one place only. History stays: it is the reason the profile
    survives at all.
    """
    removed = []
    candidates = [credentials.cache_path(provider, account)]
    if getattr(provider, "CREDENTIALS", "file") != "env":
        candidates.append(os.path.join(profile.profile_dir(provider, account), provider.AUTH_NAME))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError as exc:
            warn("could not remove %s: %s" % (path, exc))
    return removed
