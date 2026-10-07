"""What is specific to codex: where its profile lives, how to read its limits,
and how to make it route its own refresh through the broker.

Everything else in the package is provider-agnostic and works off this table.
"""

import os
import re
import urllib.parse

from . import codex_db, codex_usage

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
BOX_SETTINGS = ("~/.codex/config.toml", "~/.codex/hooks.json")
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
# Nothing here any more: the databases are no longer shared to begin with —
# each window has its own set, by way of CODEX_SQLITE_HOME below — so there is
# nothing for a box to clone. Cloning them was the old answer to the same
# problem and it cost what clones cost: a box's work went into a copy that died
# with the container, and a file mounted over could not be renamed, so the
# harness could not put a damaged one aside and refused to start at all.
BOX_PRIVATE = ()

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

# And how to be shown the list, when this run did not name its own.
SESSION_PICK = "resume"

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

# The lock directory that goes WITH that file. It exists so two processes do
# not refresh the same token at once — and kept per profile it does nothing at
# all: several accounts run side by side here, they share the token file, and
# without a shared lock two of them refresh together, one wins, and the other
# is left holding a refresh token the server has just revoked. Which is exactly
# how the login was lost twice in one day.
LOCKED_TOGETHER = ("mcp-oauth-locks",)

SHARED_GLOBS = ("*.sqlite",)

# Invocations that must reach the real binary untouched. Two kinds: things about
# the installation or the login itself (picking an account for `logout` would
# revoke whichever account the picker landed on), and purely local questions —
# asking for --help must not depend on the broker, or the network, being up.
PASSTHROUGH = ("login", "logout", "update", "--help", "-h", "--version", "-V", "help")


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

# What this harness was left with when it stopped, taken out of the session it
# just wrote.
#
# It draws its screen on the terminal's alternate buffer, so everything it ever
# showed — including the line saying the account is out of room — is wiped the
# moment it exits and the old screen comes back. What you are left looking at
# is a bare prompt, as if it had quit for no reason and said nothing. The
# account was spent an hour earlier and every turn since had been refused.
#
# The session file keeps what the screen did not, so it is read back and said
# again in the ordinary terminal, where it stays.
SQLITE_ENV = codex_db.SQLITE_ENV
session_error = codex_db.session_error
usage_request = codex_usage.usage_request
read_usage = codex_usage.read_usage


def harness_env(env):
    """Point this window's harness at its own databases."""
    return codex_db.harness_env(env, CANONICAL_HOME, HOME_ENV)