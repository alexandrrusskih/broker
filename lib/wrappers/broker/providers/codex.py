"""What is specific to codex: where its profile lives, how to read its limits,
and how to make it route its own refresh through the broker.

Everything else in the package is provider-agnostic and works off this table.
"""

import os
import re
import urllib.parse
import urllib.request

NAME = "codex"
BIN = "codex"
CMD = "broker-cx"

# codex keeps auth in a file inside its config dir, and rotates the token itself
# — hence the profile-per-account layout and the override hooks below.
CREDENTIALS = "file"
HOME_ENV = "CODEX_HOME"
CANONICAL_HOME = os.path.expanduser("~/.codex")
AUTH_NAME = "auth.json"

# Where the real binary lives, in preference order. The standalone install is
# what a Mac has (`current` is what codex's own updater moves, so the path
# survives updates). In container images codex comes from npm, and there the
# thing on PATH is a Node launcher that re-invokes `codex` by name at startup —
# under a shim that loops forever. So the vendored platform binary is preferred
# over the launcher: it is the actual program, and starting it directly skips
# both the Node process and the loop.
# What comes into a --box with this harness: its settings and history. The
# per-account profile is mounted separately (see box.py) — that is where the
# credentials this run uses live.
BOX_HOME = ("~/.codex",)
BOX_SECRETS = ("~/.codex/auth.json",)
MCP_CONFIG = ("~/.codex/config.toml", "toml", "mcp_servers")

# Sessions, in one pile per config directory rather than per project: the file
# is named for when it started, with the id at the end. A box reads the newest
# one written while it was running, so a leaving box can print a resume line
# that includes the box — the harness prints its own, and that one reopens the
# session on the host instead.
SESSION_GLOB = "%(config)s/sessions/*/*/*/rollout-*.jsonl"

# Sessions are shared across accounts already — every profile's `sessions` is a
# symlink into the canonical home — but the harness records the path it saw,
# through whichever profile was current. Resuming under another account then
# fails with "no rollout found", though the file is right there. So a box gets
# that one directory from EVERY profile: the sessions, and nothing else of
# someone else's account.
BOX_SHARED = ("sessions",)

# The databases are the opposite: a box works on its own clone of each.
#
# They are SQLite, and file locks do not cross the boundary into the container —
# a box holding an exclusive lock is invisible to the harness running here, so
# both write at once and the file tears. Measured, then seen three times in one
# afternoon: "database disk image is malformed", "file is not a database",
# "row missing from index".
#
# Nothing is lost by working on a clone, because the work itself is in the
# session files, which stay shared. What the box adds is put back on the way
# out by the harness's own command — see BOX_SYNC.
BOX_PRIVATE = ("*.sqlite",)

# Nothing is folded back on the way out, and that is deliberate.
#
# A thread started in a box is recorded in the box's copy of the database, and
# that record dies with the copy: out here the session file exists but the
# thread is in no list. `migrate-rollouts` refuses it — "missing its SQLite
# metadata" — because the metadata went with the copy.
#
# Archiving the session and unarchiving it does register the thread, and it was
# tried here. It is a race: between the two commands the session IS archived,
# and a box starting in that instant clones a database that says so. The box
# then refuses to reopen its own session — "Failed to unarchive session" — over
# a thread the host considers perfectly live. Seen within the hour.
#
# So the session file is left as the record, which it already is. Opening it
# once out here registers the thread as a side effect, and the line the box
# prints on the way out is exactly that command.
BOX_SYNC = ()


SESSION_RESUME = "resume %s"

REAL_BINS = (
    os.path.expanduser("~/.codex/packages/standalone/current/bin/codex"),
    "/usr/local/lib/node_modules/@openai/codex/bin/codex.js",
    "/usr/lib/node_modules/@openai/codex/bin/codex.js",
)
REAL_BIN = REAL_BINS[0]  # kept for callers that want the canonical one

# The npm package vendors the real executable per platform; the launcher above
# only locates it and spawns it.
VENDOR_GLOBS = (
    # `codex update` on Apple Silicon installs the npm package here. Keep its
    # native executable ahead of an older standalone/current binary.
    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex",
    # Developer installs commonly live under nvm rather than /usr/local.
    os.path.expanduser(
        "~/.nvm/versions/node/*/lib/node_modules/@openai/codex/"
        "node_modules/@openai/codex-*/vendor/*/bin/codex"
    ),
    "/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex",
    "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex",
)


def vendored_bins():
    """Platform binaries shipped inside the npm package, if any."""
    import glob

    found = []
    for pattern in VENDOR_GLOBS:
        found.extend(sorted(glob.glob(pattern)))
    return found


# Free, and it consumes no quota: the same account snapshot the codex TUI shows
# under /status.
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# Symlinked into every per-account profile: the 1.7G runtime, the settings, and
# the history — so any account picks up where the last one left off. Only the
# credentials stay per-account.
#
# sqlite is safe to share this way: it resolves the symlink and puts -wal/-shm
# beside the REAL database, so profiles opening it through different links land
# on one file and one WAL — ordinary multi-process sqlite, not corruption.
# What belongs to ONE account, and therefore must NOT be shared. Everything
# else in the harness's directory is: sessions, databases, config, hooks,
# skills, prompts, tokens for other services, the lot.
#
# Said this way round on purpose. A list of what to SHARE is always one item
# behind — the harness invents a file, nobody adds it to the list, and the next
# account quietly gets a copy of its own. That is exactly how a login granted
# in the morning came back as "authentication required" in the afternoon: the
# broker had moved to a second account because the first ran out of room, and
# the MCP tokens had stayed behind with the first.
PRIVATE = (
    "auth.json",   # the account's own credentials — the whole point of a profile
)

# Shared, but as a second NAME rather than a link. This harness opens its MCP
# token file refusing to follow symlinks — a guard against being handed someone
# else's — and reports "too many levels of symbolic links" for a single one. It
# writes into the file it already has rather than replacing it, so a hard link
# holds: one file, six names, and a token refreshed under one account is
# refreshed under all of them at once. Copies would not do — OAuth rotates the
# refresh token, so five profiles refreshing their own copies would leave four
# of them holding something the server has already forgotten.
HARD_LINKED = (".credentials.json",)

SHARED_GLOBS = ("*.sqlite",)

# Invocations that must reach the real binary untouched. Two kinds: things about
# the installation or the login itself (picking an account for `logout` would
# revoke whichever account the picker landed on), and purely local questions —
# asking for --help must not depend on the broker, or the network, being up.
PASSTHROUGH = ("login", "logout", "update", "--help", "-h", "--version", "-V", "help")


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


def read_usage(usage):
    """Normalise the provider's answer into the shape the engine ranks on."""
    rate = (usage or {}).get("rate_limit") or {}
    windows = [w for w in (rate.get("primary_window"), rate.get("secondary_window")) if w]
    row = {
        "email": usage.get("email") or "?",
        "plan": usage.get("plan_type") or "?",
        "blocked": bool(rate.get("limit_reached")) or rate.get("allowed") is False,
        "used": None,
        "window": None,
        "resets_in": None,
    }
    if windows:
        tightest = max(windows, key=lambda w: w.get("used_percent") or 0)
        row["used"] = tightest.get("used_percent") or 0
        row["window"] = tightest.get("limit_window_seconds")
        row["resets_in"] = tightest.get("reset_after_seconds")
    return row


def is_handle(auth):
    """True when the broker handed us an opaque handle, not a real token.

    A handle is a 64-char hex HMAC; a real codex refresh_token never is. This is
    what lets the wrapper self-coordinate with the broker's rollout flag without
    any extra configuration.
    """
    if not isinstance(auth, dict):
        return False
    token = (auth.get("tokens") or {}).get("refresh_token") or ""
    return bool(re.fullmatch(r"[0-9a-f]{64}", token))


def route_refresh(env, cfg, account, auth):
    """Point codex's own refresh/revoke at the broker — only when we hold a handle.

    While the rollout flag is off the broker returns the real token and codex must
    keep refreshing against OpenAI, so the overrides are actively removed: a real
    token posted to /oauthRefresh would 401. Any override inherited from a
    contaminated parent shell goes the same way.
    """
    if not is_handle(auth):
        env.pop("CODEX_REFRESH_TOKEN_URL_OVERRIDE", None)
        env.pop("CODEX_REVOKE_TOKEN_URL_OVERRIDE", None)
        return
    query = urllib.parse.urlencode({"provider": NAME, "account": account})
    env["CODEX_REFRESH_TOKEN_URL_OVERRIDE"] = "%s/oauthRefresh?%s" % (cfg["url"], query)
    env["CODEX_REVOKE_TOKEN_URL_OVERRIDE"] = "%s/oauthRevoke?%s" % (cfg["url"], query)


def login_env(env):
    """`codex login` must talk to REAL OpenAI, never the broker."""
    env.pop("CODEX_REFRESH_TOKEN_URL_OVERRIDE", None)
    env.pop("CODEX_REVOKE_TOKEN_URL_OVERRIDE", None)
    return env


def login_cmd(device):
    return [BIN, "login"] + (["--device-auth"] if device else [])
