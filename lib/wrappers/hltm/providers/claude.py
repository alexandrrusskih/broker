"""What is specific to claude (Claude Code): how it takes credentials, how to
read its limits, and how an account is logged in.

Everything else in the package is provider-agnostic and works off this table.
"""

import datetime
import json
import os
import subprocess
import sys

NAME = "claude"
BIN = "claude"
CMD = "broker-cl"

# The one provider that needs no profile. codex and agy read a credentials FILE,
# so each account has to own a directory — and everything else in that directory
# then has to be linked back to keep history and settings shared. claude reads a
# long-lived token from the environment instead, so ~/.claude stays a single
# directory for every account: MCP servers, projects, history and plugins are
# common without a single symlink.
CREDENTIALS = "env"
ENV_NAME = "CLAUDE_CODE_OAUTH_TOKEN"

# An API key in the environment outranks the OAuth token, so a stray one would
# quietly bill the wrong thing while looking like it worked.
CLEAR_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# Unused by the env path, but the engine still reads them for messages and for
# the account-name check.
HOME_ENV = "CLAUDE_CONFIG_DIR"
CANONICAL_HOME = os.path.expanduser("~/.claude")
AUTH_NAME = ".credentials.json"

# The installed program itself, not the launcher name: ~/.local/bin/claude is
# exactly where the shim goes, so resolving by that path would find the shim.
# In a container claude comes from npm, and the shim takes /usr/local/bin/claude —
# the very path npm's launcher occupies. So the package's own entry point has to be
# reachable directly, or the wrapper finds nothing but itself.
# The name inside the package is not stable — the medulla image ships
# bin/claude.exe, other builds ship cli.js — so match what is there rather than
# guessing which one this install has.
NPM_GLOBS = (
    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/*",
    "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/*",
    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
)


def vendored_bins():
    """Installed claude programs, newest first: native versions, then npm."""
    import glob

    found = glob.glob(os.path.expanduser("~/.local/share/claude/versions/*"))
    native = sorted((f for f in found if os.access(f, os.X_OK)), reverse=True)
    packaged = []
    for pattern in NPM_GLOBS:
        packaged += [f for f in sorted(glob.glob(pattern)) if os.access(f, os.X_OK)]
    return native + packaged


REAL_BINS = (
    "/usr/local/bin/claude",
    "/usr/bin/claude",
    os.path.expanduser("~/.local/bin/claude"),
)
REAL_BIN = REAL_BINS[0]

# There are no numbers to read. `/api/oauth/usage` carries the five-hour and
# seven-day windows, but it demands the `user:profile` scope, and a setup-token
# is issued with `user:inference` and nothing else — so a year-long token can
# spend the quota and never see it. Verified: 403, required_scopes user:profile.
#
# What IS available on that scope answers the question that actually matters
# before a run: is this token still accepted, and is the account inside its
# limit? `count_tokens` says both, costs no quota, and returns 429 exactly when
# the account has run out.
USAGE_URL = "https://api.anthropic.com/v1/messages/count_tokens"
PROBE_MODEL = "claude-sonnet-4-6"

# Anything about the installation or the login itself, plus purely local
# questions that must work with no broker and no network.
PASSTHROUGH = (
    "setup-token",
    "install",
    "update",
    "doctor",
    "migrate-installer",
    "--help",
    "-h",
    "--version",
    "-v",
    "help",
)


def env_token(auth):
    """The token to hand the harness, whichever shape the broker answered in."""
    if not isinstance(auth, dict):
        return None
    oauth = auth.get("claudeAiOauth") or {}
    return oauth.get("accessToken") or auth.get("token") or auth.get("access_token")


def _seconds_until(when):
    """Seconds until an instant given as epoch seconds or an ISO-8601 string."""
    if not when:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if isinstance(when, (int, float)):
        moment = float(when) / (1000 if when > 1e11 else 1)
        return max(0, int(moment - now.timestamp()))
    try:
        parsed = datetime.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return max(0, int((parsed - now).total_seconds()))


# How the limits are actually visible. The usage API needs `user:profile` and a
# setup-token carries only `user:inference` — so a year-long token can spend the
# quota and never read it (verified: 403, required_scopes user:profile). The
# harness itself, however, emits the numbers as a `rate_limit_event` in its
# stream: status, utilization, which window, and when it resets. So the probe IS
# a tiny run of claude, and its answer is cached — a run costs a few tokens and a
# couple of seconds, which is fine every few minutes and absurd every invocation.
PROBE_TTL = 600
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
PROBE_PROMPT = "hi"


def _windows(info):
    """Every window the event describes, as (name, {utilization, resetsAt}).

    The harness only lifts a window to the top level of rate_limit_info once it
    is worth warning about; an account with room reports `status: allowed` and
    keeps its numbers in `unifiedWindows` alone. Reading only the top level
    therefore measured accounts near their limit and left every quiet account
    looking unmeasured — which is exactly backwards.
    """
    windows = []
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        windows = [(name, w) for name, w in unified.items() if isinstance(w, dict)]
    if info.get("utilization") is not None:
        windows.append((info.get("rateLimitType"), info))
    return windows


def _probe_cache(account):
    return os.path.join(
        os.path.expanduser("~/.config/hltm-broker/cache"), "claude-%s-usage.json" % account
    )


def _read_probe_cache(account):
    try:
        with open(_probe_cache(account)) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    if datetime.datetime.now().timestamp() - cached.get("at", 0) > PROBE_TTL:
        return None
    return cached.get("row")


def _write_probe_cache(account, row):
    path = _probe_cache(account)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"at": datetime.datetime.now().timestamp(), "row": row}, fh)
        os.chmod(path, 0o600)
    except OSError:
        pass  # a cache that cannot be written only costs speed


def probe_row(account, auth, cheap_only=False):
    """What the engine ranks on, measured by running the harness once, briefly."""
    cached = _read_probe_cache(account)
    if cached is not None:
        return cached
    if cheap_only:
        return None  # nothing cached, and measuring costs a run — say nothing

    # A panel starts several agents at once, and every one of them would find the
    # cache empty and launch its own probe — five extra runs of the harness to
    # learn one number. The first to claim the lock measures; the rest start
    # immediately without it.
    lock = _probe_cache(account) + ".lock"
    try:
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        if os.path.exists(lock) and datetime.datetime.now().timestamp() - os.path.getmtime(lock) > 180:
            os.remove(lock)  # a probe that died mid-flight must not block forever
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return None  # someone else is measuring right now
    except OSError:
        lock = None

    try:
        return _measure(account, auth, lock)
    finally:
        # Every exit releases the claim: a probe that timed out or was
        # interrupted used to leave its lock behind, and every later probe of
        # that account then reported nothing for the three minutes it took the
        # stale-lock sweep to notice.
        if lock:
            try:
                os.remove(lock)
            except OSError:
                pass


def _measure(account, auth, lock):
    row = {"email": "?", "plan": "max", "blocked": False,
           "used": None, "window": None, "resets_in": None}

    from ..run import real_bin

    binary = real_bin(sys.modules[__name__])
    if not binary:
        return row

    env = dict(os.environ)
    env[ENV_NAME] = env_token(auth) or ""
    for name in CLEAR_ENV:
        env.pop(name, None)
    try:
        done = subprocess.run(
            [binary, "-p", PROBE_PROMPT, "--output-format", "stream-json", "--verbose"],
            env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return row
    except KeyboardInterrupt:
        # Ctrl-C during a probe should end the probe, not be swallowed by it.
        raise

    for line in done.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            for name, window in _windows(info):
                used = window.get("utilization")
                if not isinstance(used, (int, float)):
                    continue
                used = used * (100 if used <= 1 else 1)
                # The tighter window wins, exactly as it does for codex.
                if row["used"] is None or used > row["used"]:
                    row["used"] = int(round(used))
                    # The table renders this as a duration, so give it the
                    # window's length rather than its name.
                    row["window"] = WINDOW_SECONDS.get(name)
                    row["resets_in"] = _seconds_until(window.get("resetsAt"))
            if info.get("status") == "rejected":
                # A rejection carries no utilization — it is past that — but it
                # does carry when the window reopens, which is the thing you
                # actually want to know about an account you cannot use.
                row["blocked"] = True
                row["used"] = 100
                row["window"] = WINDOW_SECONDS.get(info.get("rateLimitType"))
                row["resets_in"] = _seconds_until(info.get("resetsAt"))
        if event.get("type") == "result" and event.get("api_error_status") == 429:
            row["blocked"] = True
            row["used"] = 100

    _write_probe_cache(account, row)
    return row


def route_refresh(env, cfg, account, auth):
    """Nothing to route: a setup-token does not refresh and does not rotate.

    It is issued for a year and used as-is, so the broker is a place to keep it
    and hand it out, not a refresh authority. That also means no second holder
    can invalidate it — the failure that made all of this necessary for codex
    cannot happen here.
    """
    return


def login_env(env):
    """`claude setup-token` must talk to Anthropic under whoever is logging in."""
    for name in CLEAR_ENV + (ENV_NAME,):
        env.pop(name, None)
    return env


def login_cmd(_device):
    """Mint a year-long token; it is printed at the end of the flow."""
    return [BIN, "setup-token"]


def read_local_auth(_home):
    """No local auth file to read: the token lives in the broker."""
    return None
