"""The credentials inside a profile, and the cache that stands in for them."""

import base64
import json
import os

from . import config
from .out import warn
from .layout import prepare
from .profile import profile_dir


def write_auth(provider, path, auth):
    """Install the auth file atomically and 0600 — a half-written token file
    would otherwise replace a working one on a dropped connection."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    prepare(provider, path)
    # AUTH_NAME may be a path (agy keeps its token three levels down), so the
    # temp file has to be written beside the destination, not at the top.
    target = os.path.join(path, provider.AUTH_NAME)
    auth = _keep_what_the_broker_cannot_send(provider, target, auth)
    config.write_json(target, auth, prefix=".auth-")


def _keep_what_the_broker_cannot_send(provider, target, auth):
    """Carry forward the fields a refresh never returns and the broker lacks.

    agy's consumer auth refuses a token file with no id_token — "You are not
    logged into Antigravity" — and a refresh usually sends none back. So the
    field comes from whoever logged in, and this file is rewritten whole on
    every run: without carrying it, the first run after a login threw away the
    one thing that made the login usable.

    It stayed hidden because a machine that had ever run agy natively had an
    id_token of its own in the canonical home and never noticed. A machine set
    up from the broker alone had nothing to fall back on: an account that
    `broker accounts` listed, a token file 922 characters shorter, and a
    harness that would not start.

    The broker's own value does NOT simply win. It holds the id_token it was
    seeded with and hands that same copy back, while only a login mints a new
    one — so after a fresh login the broker's copy is the OLDER of the two, and
    letting it win undid the login on the very next run. Measured here: a
    successful sign-in at 19:54, a run at 19:56, and a token file carrying the
    id_token from 19:13 again, with agy reporting the sign-in as out of date
    and no way to make the message go away.

    So a carried field wins when it lives longer. For a JWT that is its `exp`;
    where neither copy is one, the new value stands, as before.
    """
    keep = getattr(provider, "AUTH_KEEP", ())
    if not keep or not isinstance(auth, dict):
        return auth
    try:
        with open(target) as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        return auth
    if not isinstance(previous, dict):
        return auth
    carried = dict(auth)
    for field in keep:
        kept = previous.get(field)
        if not kept:
            continue
        if not carried.get(field) or _outlives(kept, carried[field]):
            carried[field] = kept
    return carried


def _outlives(candidate, current):
    """Whether `candidate` is a token valid for longer than `current`."""
    mine, theirs = _jwt_expiry(candidate), _jwt_expiry(current)
    if mine is None or theirs is None:
        return False
    return mine > theirs


def _jwt_expiry(value):
    """The `exp` claim of a JWT, or None when the value is not one.

    The signature is not checked, and does not need to be: the question here is
    which of two copies of the same field the harness should be handed, and
    both arrived from somewhere already trusted to write this file.
    """
    if not isinstance(value, str) or value.count(".") != 2:
        return None
    body = value.split(".")[1]
    body += "=" * (-len(body) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    expiry = claims.get("exp")
    return expiry if isinstance(expiry, (int, float)) else None


def _cache_dir():
    """Where cached credentials go.

    Beside the config normally — but that whole tree is mounted READ-ONLY inside
    medulla's container, and a cache that cannot be written must not look like a
    failure. Fall back to the temp dir, which every container has.
    """
    preferred = config.CACHE_DIR
    try:
        os.makedirs(preferred, mode=0o700, exist_ok=True)
        if os.access(preferred, os.W_OK):
            return preferred
    except OSError:
        pass
    fallback = os.path.join(os.environ.get("TMPDIR", "/tmp"), "broker-cache-%d" % os.getuid())
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    return fallback


CACHE_DIR = config.CACHE_DIR


def cache_path(provider, account):
    return os.path.join(_cache_dir(), "%s-%s.json" % (provider.NAME, account))


def write_cache(provider, account, auth):
    """Keep the broker's answer for a provider that has no profile on disk.

    claude takes its credentials through the environment, so there is no auth
    file to fall back on when the broker cannot be reached. This is that
    fallback, and nothing else reads it.
    """
    _cache_dir()  # the fallback directory, made before anything is written into it
    config.write_json(cache_path(provider, account), auth, prefix=".cache-")


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
