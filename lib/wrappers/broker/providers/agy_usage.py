"""What an account has left, as Antigravity itself reports it."""

import datetime
import json
import os
import urllib.request


# Antigravity's own quota view — the same numbers its IDE shows, free to ask for
# and charged to nothing. `models` carries a remaining fraction per model, which
# is a richer answer than codex's single window.
USAGE_URL = "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
USAGE_AGENT = "antigravity/%s/%s" % (os.uname().sysname.lower(), os.uname().machine)

# The models a run actually lands on. A rank driven by every model in the list
# would let an exhausted image or tab-completion bucket veto an account whose
# chat quota is untouched.
RANKED_PREFIXES = ("gemini-3", "claude-")


def _post(url, auth, body=b"{}"):
    """One signed POST to Antigravity's own API. Both calls are shaped alike."""
    token = (auth or {}).get("token") or {}
    return urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + (token.get("access_token") or ""),
            "Content-Type": "application/json",
            "User-Agent": USAGE_AGENT,
        },
    )


def usage_request(auth):
    """A request for this account's remaining quota per model."""
    return _post(USAGE_URL, auth)


def _reset_seconds(when):
    """Seconds until an RFC-3339 reset time, or None if it is unparseable."""
    if not when:
        return None
    text = when.replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return max(0, int((moment - now).total_seconds()))


def read_usage(usage):
    """Normalise the provider's answer into the shape the engine ranks on.

    Antigravity reports what is LEFT per model; the engine ranks on what is
    USED, and on the tightest bucket — so the model closest to empty is the one
    that decides, exactly as codex's tightest window does.
    """
    models = (usage or {}).get("models") or {}
    row = {
        "email": "?",  # this endpoint does not carry one, and asking costs a round-trip
        "plan": "antigravity",
        "blocked": False,
        "used": None,
        "window": None,
        "resets_in": None,
    }

    tracked = []
    for name, model in models.items():
        info = (model or {}).get("quotaInfo") or {}
        if "remainingFraction" not in info:
            continue
        if not name.startswith(RANKED_PREFIXES):
            continue
        fraction = info.get("remainingFraction")
        if not isinstance(fraction, (int, float)):
            continue
        tracked.append((max(0.0, min(1.0, float(fraction))), info.get("resetTime")))

    if not tracked:
        return row

    remaining, reset = min(tracked, key=lambda item: item[0])
    row["used"] = int(round((1 - remaining) * 100))
    row["blocked"] = remaining <= 0
    row["resets_in"] = _reset_seconds(reset)
    return row


# Antigravity is a product tier ("free-tier"), and a Google account is admitted to
# it by the country ON THE ACCOUNT — not by where the request comes from, and not
# by whose family subscription pays for it. An account that is refused still
# answers the quota endpoint perfectly happily, so without this check the picker
# sees a healthy account with plenty left, sends work there and every run dies on
# `Eligibility check failed`.
ELIGIBILITY_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
PRODUCT_TIER = "free-tier"


def check_eligibility(auth):
    """Why this account cannot be used, or None if it can."""
    body = json.dumps(
        {"metadata": {"ideType": "IDE_UNSPECIFIED", "platform": "DARWIN_ARM64", "pluginType": "GEMINI"}}
    ).encode()
    request = _post(ELIGIBILITY_URL, auth, body)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            answer = json.load(resp)
    except Exception:  # noqa: BLE001 — an unreachable check must not veto an account
        return None
    for tier in answer.get("ineligibleTiers") or []:
        if tier.get("tierId") == PRODUCT_TIER:
            reason = tier.get("reasonCode") or "ineligible"
            return "not eligible for Antigravity (%s)" % reason.lower().replace("_", " ")
    return None
