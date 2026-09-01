"""Probing accounts and ranking them. Provider-agnostic: the provider module
turns its own API answer into the shape used here."""

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import api, config

TIMEOUT = 15


def stub(account):
    return {
        "account": account,
        "auth": None,
        "email": "?",
        "plan": "?",
        "used": None,
        "window": None,
        "resets_in": None,
        "blocked": False,
        "error": None,
    }


def probe(cfg, provider, account, cheap_only=False):
    """What the pick needs to judge one account. Never raises.

    `cheap_only` refuses to pay for the answer: claude has no usage endpoint its
    token may read, so measuring it means RUNNING the harness. That is fine on
    the auto-pick path, where the result is cached and reused; it is not fine
    when the caller already named the account and is waiting to start.
    """
    row = stub(account)
    try:
        row["auth"] = api.fetch_auth(cfg, provider, account)
    except api.Unreachable:
        raise  # not this account's fault — the caller decides whether to fall back
    except RuntimeError as exc:
        row["error"] = str(exc)
        return row

    # A provider whose numbers do not come from an HTTP endpoint answers for
    # itself. claude is one: its limits are only visible in the event stream of
    # the harness, since the token it runs on cannot read the usage API.
    own = getattr(provider, "probe_row", None)
    if own:
        measured = own(account, row["auth"], cheap_only=cheap_only)
        if measured is not None:
            row.update(measured)
        return row

    try:
        with urllib.request.urlopen(provider.usage_request(row["auth"]), timeout=TIMEOUT) as resp:
            usage = json.load(resp)
    except urllib.error.HTTPError as exc:
        # The broker still hands out this token happily — only the provider knows
        # it was revoked (a logout elsewhere, or a refresh that raced the broker
        # and tripped reuse detection).
        # 401 means the provider itself rejected the token the broker holds: it
        # was revoked elsewhere, or it simply reached the end of its life (a
        # claude setup-token lasts a year). Either way the account cannot be
        # used until someone acts, so say which two actions exist.
        row["error"] = (
            "token rejected — run '%s auth %s' to mint a new one, or "
            "'broker forget %s --account %s --yes' to drop it"
            % (provider.CMD, account, provider.NAME, account)
            if exc.code == 401
            else "usage unavailable: HTTP %s" % exc.code
        )
        return row
    except (urllib.error.URLError, OSError, ValueError) as exc:
        row["error"] = "usage unavailable: %s" % exc
        return row

    row.update(provider.read_usage(usage))

    # Quota is not the only way an account can be unusable: agy answers the quota
    # endpoint for accounts its product will refuse outright. A provider that can
    # tell says so here, so the picker never hands work to one of them.
    gate = getattr(provider, "check_eligibility", None)
    if gate:
        problem = gate(row["auth"])
        if problem:
            row["error"] = problem
    return row


def probe_all(cfg, provider, names):
    """Probe every account, every time — limits are only worth acting on while
    they are current, and a stale pick sends work to an exhausted account."""
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(names)))) as pool:
        rows = list(pool.map(lambda n: probe(cfg, provider, n), names))
    rows.sort(key=rank)
    return rows


def headroom(row):
    """Percent of the window still free.

    Unknown is not empty. A provider may expose no usage numbers at all — claude
    spends its quota through a token whose scope cannot read it — and treating
    that as "nothing left" made the sticky default never hold: your own account
    looked exhausted on every run, and work drifted onto someone else's.
    """
    return 100 - (row["used"] or 0)


def has_room(cfg, row):
    return not row["error"] and not row["blocked"] and headroom(row) >= config.min_headroom(cfg)


def rank(row):
    """Usable first, then the most headroom, then the soonest reset."""
    used = row["used"] or 0
    return (
        1 if row["error"] else 0,
        1 if row["blocked"] else 0,
        used,
        row["resets_in"] if row["resets_in"] is not None else 0,
    )


def would_pick(cfg, provider, rows):
    """Which account a bare run lands on — the same rules `select` uses."""
    home = config.home_account(cfg, provider.NAME)
    if home:
        row = next((r for r in rows if r["account"] == home), None)
        if row and has_room(cfg, row):
            return home
    usable = [r for r in rows if not r["error"]]
    return usable[0]["account"] if usable else None
