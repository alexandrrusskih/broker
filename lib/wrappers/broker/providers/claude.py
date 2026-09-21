"""What is specific to claude (Claude Code): how it takes credentials, how to
read its limits, and how an account is logged in.

Everything else in the package is provider-agnostic and works off this table.
"""

import datetime
import json
import os
import subprocess
import sys

from .. import config

NAME = "claude"
BIN = "claude"
CMD = "broker-cl"

# This harness resolves its own binary by path, never by name, so it needs no
# PATH shield — and shielding it would take the bare command off the broker
# inside a session started by the wrapper itself.
PATH_SHIELD = False

# The one provider that needs no profile. codex and agy read a credentials FILE,
# so each account has to own a directory — and everything else in that directory
# then has to be linked back to keep history and settings shared. claude reads a
# long-lived token from the environment instead, so ~/.claude stays a single
# directory for every account: MCP servers, projects, history and plugins are
# common without a single symlink.
CREDENTIALS = "env"
ENV_NAME = "CLAUDE_CODE_OAUTH_TOKEN"

# What comes into a --box with this harness. The directory travels WHOLE:
# settings, MCP servers, agents, commands, plugins, projects and history. Picking
# parts of it would silently drop whatever the next release adds — and the
# session history is keyed by the project's absolute path, which the box
# preserves, so `--resume` inside the box finds the same sessions.
BOX_HOME = ("~/.claude", "~/.claude.json")
# The account's own token never travels: it arrives in the environment from the
# broker (CREDENTIALS = "env") and is never written to disk here at all.
#
# .credentials.json is NOT that token. It holds mcpOAuth — the logins for the
# MCP servers a box talks to, which are the same services whichever account is
# picked. Handing the box a read-only empty file in its place meant every box
# started logged out of all of them, with nowhere to save a new login: the
# session was gone again at the next start. So it travels like the rest of the
# directory, and a login inside a box is a login everywhere.
BOX_SECRETS = ()
# Where this harness declares its MCP servers, so a box can reach the ones that
# only exist on this machine (see mcpbridge.py).
MCP_CONFIG = ("~/.claude.json", "json", "mcpServers")

# Sessions, keyed by the working directory with its separators turned into
# dashes. A box reads the newest one on the way out, to print a resume line that
# includes the box — the harness prints its own, and that one reopens the
# session on the host instead.
SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"

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


def _chosen_version():
    """The version the launcher pointed at before the shim took its place.

    `claude install stable` and `claude install latest` differ only in which
    version they link from ~/.local/bin/claude — the older ones stay on disk. So
    picking the highest number on disk quietly ignores that choice: after asking
    for stable you would still run whatever `latest` left behind.
    """
    try:
        with open(config.path()) as fh:
            remembered = (json.load(fh).get("shim_previous") or {}).get("claude")
    except (OSError, ValueError):
        return None
    if remembered and os.access(remembered, os.X_OK):
        return remembered
    return None


def vendored_bins():
    """Installed claude programs: the chosen one first, then newest to oldest."""
    import glob

    found = glob.glob(os.path.expanduser("~/.local/share/claude/versions/*"))
    native = sorted((f for f in found if os.access(f, os.X_OK)), reverse=True)
    chosen = _chosen_version()
    if chosen:
        native = [chosen] + [f for f in native if f != chosen]
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
# A measurement that came back empty is worth remembering only briefly. Writing
# it for the full TTL is how a single failed probe made an account show dashes
# for ten minutes while it was perfectly healthy — and four probes racing each
# other over one ~/.claude is exactly when that happens.
EMPTY_TTL = 60
# A probe takes seconds; a lock older than this belongs to a run that died.
LOCK_STALE = 90
# Probing claude means RUNNING claude, and parallel runs contend over the same
# state directory. Two at a time measures four accounts quickly enough without
# them tripping over each other.
PROBE_CONCURRENCY = 2
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
        config.CACHE_DIR, "claude-%s-usage.json" % account
    )


# Cache disabled by request: every answer is measured when it is asked for.
# The cost is real and deliberate — probing claude means starting claude, so a
# bare `claude` now waits for that before it begins. In exchange no number is
# ever older than the question, which is what the cache kept getting wrong:
# empty measurements stuck around, and a stale figure looked fresh.
CACHE_ENABLED = False


def _read_probe_cache(account):
    if not CACHE_ENABLED:
        return None
    try:
        with open(_probe_cache(account)) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    row = dict(cached.get("row") or {})
    age = datetime.datetime.now().timestamp() - cached.get("at", 0)
    ttl = PROBE_TTL if row.get("used") is not None else EMPTY_TTL
    if age > ttl:
        return None
    # How old the answer is, so a caller can say so instead of presenting a
    # nine-minute-old number as if it were just measured.
    row["age"] = int(age)
    return row


def _write_probe_cache(account, row):
    if not CACHE_ENABLED:
        return
    path = _probe_cache(account)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"at": datetime.datetime.now().timestamp(), "row": row}, fh)
        os.chmod(path, 0o600)
    except OSError:
        pass  # a cache that cannot be written only costs speed


def probe_row(account, auth, cheap_only=False, fresh=False):
    """What the engine ranks on, measured by running the harness once, briefly.

    `fresh` ignores the cache. Asking "what is left right now" must never answer
    from something measured ten minutes ago — that is the whole point of asking.
    The run path still uses the cache: there a stale number costs nothing, while
    measuring would add a harness start to every single invocation.
    """
    cached = None if fresh else _read_probe_cache(account)
    if cached is not None:
        return cached
    if cheap_only:
        return None  # nothing cached, and measuring costs a run — say nothing

    # The lock only made sense alongside the cache: one process measured, the
    # others read its answer. With no cache there is nothing to read, so a lock
    # would just hand them an empty row — which is exactly how a freshly started
    # run reported "this token cannot read usage" while the account was fine.
    # Everyone measures for themselves now; slower, but never blank.
    lock = None
    if CACHE_ENABLED:
        lock = _probe_cache(account) + ".lock"
    try:
        if lock is None:
            raise OSError  # no lock in use — go straight to measuring
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        if os.path.exists(lock) and datetime.datetime.now().timestamp() - os.path.getmtime(lock) > LOCK_STALE:
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

    # claude reports a limit only once it crosses a warning threshold — 75% for
    # the weekly window, 90% for the five-hour one. Below that it says nothing,
    # and an empty row then reads as "no data / broken" when it actually means
    # "comfortably under the line". Remember that the run itself succeeded.
    answered = False
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
        if event.get("type") == "result" and not event.get("is_error"):
            answered = True
        if event.get("type") == "result" and event.get("api_error_status") == 429:
            row["blocked"] = True
            row["used"] = 100

    # Nothing reported, but the harness answered: that is "below the warning
    # threshold", not "unknown".
    if row["used"] is None and answered:
        row["below_threshold"] = True
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
