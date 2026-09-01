"""Talking to the broker. Every action is a path under its base URL."""

import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15


class Unreachable(RuntimeError):
    """The broker did not answer. Distinct from a rejection: an account that is
    genuinely dead must not be papered over with a cached token."""


def call(cfg, endpoint, method="GET", **params):
    url = "%s/%s?%s" % (cfg["url"], endpoint, urllib.parse.urlencode(params))
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method, headers={"x-broker-key": cfg["key"]})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def list_accounts(cfg, provider, die):
    """Every account seeded for this provider, straight from the broker."""
    try:
        return call(cfg, "listAccounts", provider=provider).get("accounts") or []
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            die("this broker has no /listAccounts — deploy it first")
        die("listAccounts failed (%s) — %s" % (exc.code, exc.reason))
    except urllib.error.URLError as exc:
        # Raised, not reported: the caller may still have a cached token to run
        # on, and only it knows whether falling back is appropriate.
        raise Unreachable("cannot reach the broker: %s" % exc.reason)


def fetch_auth(cfg, provider, account):
    """The provider's auth file, straight from the broker. Raises RuntimeError.

    `provider` is the provider module: the advice in these messages has to name
    the command the reader actually has. It used to say `cx`, which was renamed
    to broker-cx and deleted — and for agy it was never the right name at all.
    """
    try:
        return call(cfg, "getToken", provider=provider.NAME, account=account, format="authjson")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(
                "needs re-auth — run '%s auth %s'" % (provider.CMD, account)
            )
        if exc.code == 404:
            raise RuntimeError("not seeded — run '%s auth %s'" % (provider.CMD, account))
        raise RuntimeError("broker %s: %s" % (exc.code, exc.reason))
    except urllib.error.URLError as exc:
        raise Unreachable("cannot reach the broker: %s" % exc.reason)


def delete_account(cfg, provider, account, die):
    """Forget an account in the broker; returns whether it was there."""
    try:
        body = call(cfg, "deleteAccount", method="POST", provider=provider, account=account)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            die("this broker has no /deleteAccount — deploy it first")
        die("delete failed (%s): %s" % (exc.code, exc.reason))
    except urllib.error.URLError as exc:
        die("cannot reach the broker: %s" % exc.reason)
    return bool(body.get("existed"))
