"""What is specific to claude (Claude Code): how it takes credentials, how to
read its limits, and how an account is logged in.

Everything else in the package is provider-agnostic and works off this table.
"""

import json
import os

from .. import config
from . import claude_probe

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

# What the HOST runs or reads as instruction from this directory, and a box has
# no business changing. The directory itself travels writable, because that is
# where the harness keeps its state — so without this a box could rewrite a
# hook, a skill or a plugin's code, and the harness OUT HERE would run it at its
# next start. Prompt files are the quieter half of the same hole: a line added
# to one of these reaches every chat on this machine.
#
# Cache and state are deliberately left out. A box that cannot write its own
# cache is a box that does not work.
BOX_READONLY = (
    "~/.claude/CLAUDE.md",
    "~/.claude/RTK.md",
    "~/.claude/hooks",
    "~/.claude/skills",
    "~/.claude/agents",
    "~/.claude/commands",
    # The plugin code and the hooks inside it; its cache and store stay writable.
    "~/.claude/plugins/marketplaces",
    "~/.claude/plugins/data",
)
BOX_SETTINGS = ("~/.claude/settings.json",)
# Where this harness declares its MCP servers, so a box can reach the ones that
# only exist on this machine (see mcpbridge.py).
MCP_CONFIG = ("~/.claude.json", "json", "mcpServers")

# Sessions, keyed by the working directory with its separators turned into
# dashes. A box reads the newest one on the way out, to print a resume line that
# includes the box — the harness prints its own, and that one reopens the
# session on the host instead.
SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"

# Reading the newest file is only right when one harness writes there. Thirteen
# boxes open on the same project write into this one directory, so "newest"
# regularly belongs to somebody else's window, and the box would offer a resume
# line into a session the person had never seen. This harness will take the id
# as an argument, so the box names it up front and never has to guess.
SESSION_ID_FLAG = ("--session-id", "%s")

# Ways of asking for a session that already exists: the id is the person's to
# choose then, not ours. `-r` and `--continue` may also come with nothing at
# all, which opens a picker — the id is unknowable until it closes, and the box
# falls back to reading the directory.
# Without an id it opens the picker.
SESSION_PICK = "--resume"

SESSION_PICKERS = ("--resume", "-r", "--continue", "-c", "--session-id", "--from-pr")

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

# Sessions are written down per account so a resume lands on the account the
# session ran on (see sessions.py). claude takes `--session-id <uuid>`, so a new
# interactive session gets its id from the broker; a subcommand must not.
SESSION_IDS = True
SUBCOMMANDS = (
    "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import",
    "install", "logs", "mcp", "plugin", "plugins", "project", "respawn", "rm",
    "setup-token", "stop", "kill", "ultrareview", "update", "upgrade",
    "migrate-installer", "help", "config",
)


def env_token(auth):
    """The token to hand the harness, whichever shape the broker answered in."""
    if not isinstance(auth, dict):
        return None
    oauth = auth.get("claudeAiOauth") or {}
    return oauth.get("accessToken") or auth.get("token") or auth.get("access_token")


# The engine reads the provider module: this name has to be an attribute of it,
# wherever its code lives.
probe_row = claude_probe.probe_row


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
