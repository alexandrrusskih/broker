"""Reading what an account has left. Called through the provider module."""

import urllib.parse
import urllib.request


# Free, and it consumes no quota: the same account snapshot the codex TUI shows
# under /status.
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

def usage_request(auth):
    """A request for this account's plan and rate-limit snapshot."""
    tokens = auth.get("tokens") or {}
    return urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + (tokens.get("access_token") or ""),
            "chatgpt-account-id": tokens.get("account_id") or "",
            "originator": "codex_cli_rs",
        },
    )


def _balance(value):
    """The credit balance as a number. It arrives as a string ("62500")."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def read_usage(usage):
    """Normalise the provider's answer into the shape the engine ranks on."""
    rate = (usage or {}).get("rate_limit") or {}
    windows = [w for w in (rate.get("primary_window"), rate.get("secondary_window")) if w]
    credits = (usage or {}).get("credits") or {}
    resets = (usage or {}).get("rate_limit_reset_credits") or {}
    models = (usage or {}).get("model_usage") or {}
    row = {
        "email": usage.get("email") or "?",
        "plan": usage.get("plan_type") or "?",
        "blocked": bool(rate.get("limit_reached")) or rate.get("allowed") is False,
        "used": None,
        "window": None,
        "resets_in": None,
        # Reported, not acted on: the pick is unchanged. Both of these are the
        # answer to "what is left here besides the window", and both are easy to
        # read wrongly, so the table says which of them is actually reachable.
        "credits_balance": _balance(credits.get("balance")),
        # A balance is not permission to spend it. Whether credits unlock a model
        # is a property of the PLAN, and on one that already includes the model it
        # is false however much money is on the account — measured here with two
        # pro accounts holding 62500 each and `credits_would_enable: false` on
        # every model, while a prolite account with a zero balance had it true.
        # So spendable means both: the plan would take credits, and there are some.
        "credits_spendable": bool(credits.get("has_credits")) and any(
            (model or {}).get("credits_would_enable") for model in models.values()
        ),
        # A banked "Full reset" clears both windows and moves the weekly date
        # about seven days out. `available_count` is how many are BANKED;
        # `applicable_available_count` is how many can be redeemed right now,
        # which is zero until the account is actually out of room. The two are
        # different numbers and the table keeps them apart.
        "resets_held": resets.get("available_count") or 0,
        "resets_applicable": resets.get("applicable_available_count") or 0,
    }
    if windows:
        tightest = max(windows, key=lambda w: w.get("used_percent") or 0)
        row["used"] = tightest.get("used_percent") or 0
        row["window"] = tightest.get("limit_window_seconds")
        row["resets_in"] = tightest.get("reset_after_seconds")
    return row
