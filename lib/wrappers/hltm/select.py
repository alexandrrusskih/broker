"""Choosing the account for this run.

Three ways, in order: the caller names one, your own account still has room, or
whoever has the most headroom left.

The sticky default is the point of the whole thing: an account should be used
from as few places as possible, because every extra machine on it is another IP
against the same subscription. So while your account has room, the others are
not even probed — a probe carries their own tokens and would light them up from
here too.
"""

from . import accounts, api, config, profile, table
from .out import die, warn


def _named(cfg, provider, account):
    """The account the caller asked for, checked before we hand work to it.

    Naming an account used to skip the probe to save a round-trip — which meant a
    token the provider no longer accepts surfaced as a failure inside the harness,
    with no hint about what to do. One request is worth the difference between
    "run auth again" and a stack trace from someone else's program.
    """
    try:
        row = accounts.probe(cfg, provider, account, cheap_only=True)
    except api.Unreachable as exc:
        return account, _cached_or_die(provider, account, exc)
    if row["error"]:
        die("%s: %s" % (account, row["error"]))
    if row["auth"]:
        profile.ensure(provider, [row])
        return account, row["auth"]
    try:
        return account, api.fetch_auth(cfg, provider, account)
    except api.Unreachable as exc:
        return account, _cached_or_die(provider, account, exc)
    except RuntimeError as exc:
        die("%s: %s" % (account, exc))


def _cached_or_die(provider, account, exc):
    """Run on the token already in the profile when the broker is unreachable.

    Falling over entirely would make the broker a single point of failure for
    every run, on a machine where two people and CI share it. The cached token
    was valid when it was written; if it has since expired, the harness will try
    to refresh, reach the same unreachable broker, get a transient error and say
    so — a far better failure than nothing starting at all.

    Only for Unreachable: a rejected or revoked account must still fail loudly.
    """
    auth = profile.read_auth(provider, account)
    if not auth:
        die("%s: %s (and no cached credentials in the profile)" % (account, exc))
    warn("%s — using the token already in the profile; limits unknown" % exc)
    return auth


def _announce(row):
    warn(
        "%s (%s) — %s used, resets in %s"
        % (
            row["account"],
            row["plan"],
            "?" if row["used"] is None else "%d%%" % row["used"],
            table.human(row["resets_in"]),
        )
    )


def resolve(cfg, provider, explicit):
    """(account, auth) for this run."""
    if explicit:
        return _named(cfg, provider, explicit)

    home = config.home_account(cfg, provider.NAME)
    if home:
        try:
            row = accounts.probe(cfg, provider, home)
        except api.Unreachable as exc:
            return home, _cached_or_die(provider, home, exc)
        if accounts.has_room(cfg, row):
            profile.ensure(provider, [row])
            # A provider with no readable usage would otherwise announce a
            # confident "0% used, 100% left" it knows nothing about.
            if row["used"] is None:
                warn("%s (%s) — usable; this token cannot read usage" % (home, row["plan"]))
            else:
                warn(
                    "%s (%s) — %d%% used, %d%% left, resets in %s"
                    % (home, row["plan"], row["used"], accounts.headroom(row),
                       table.human(row["resets_in"]))
                )
            return home, row["auth"]
        why = row["error"] or (
            "rate-limited" if row["blocked"] else "%d%% left" % accounts.headroom(row)
        )
        warn("%s is out of room (%s) — looking elsewhere" % (home, why))

    try:
        names = api.list_accounts(cfg, provider.NAME, die)
    except api.Unreachable as exc:
        # No list, no pick — but the account this machine belongs to may still
        # have a usable token on disk.
        if home:
            return home, _cached_or_die(provider, home, exc)
        die("%s (and no account of your own to fall back on)" % exc)
    if not names:
        die("no %s accounts seeded — run '%s auth <name>'" % (provider.NAME, provider.CMD))
    if len(names) == 1:
        return _named(cfg, provider, names[0])

    rows = accounts.probe_all(cfg, provider, names)
    profile.ensure(provider, rows)

    usable = [r for r in rows if not r["error"]]
    if not usable:
        die(
            "no usable account:\n  "
            + "\n  ".join("%s — %s" % (r["account"], r["error"]) for r in rows)
        )

    best = usable[0]
    if best["blocked"]:
        warn(
            "every account is rate-limited — using %s, resets in %s"
            % (best["account"], table.human(best["resets_in"]))
        )
    else:
        _announce(best)

    if best.get("auth"):
        return best["account"], best["auth"]
    return _named(cfg, provider, best["account"])
