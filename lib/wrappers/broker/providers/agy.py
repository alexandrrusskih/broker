"""What is specific to agy (Antigravity CLI): where its profile lives, how to
read its limits, and how an account is logged in.

Everything else in the package is provider-agnostic and works off this table.
"""

import calendar
import datetime
import json
import os

from .. import config
from . import agy_usage

NAME = "agy"
BIN = "agy"
CMD = "broker-agy"

# Resolves its binary by path (the stash), so the shield is unnecessary here too.
PATH_SHIELD = False

CREDENTIALS = "file"

# What comes into a --box with this harness. The per-account profile is mounted
# by the engine (it carries the token), but the profile is a MIRROR: most of it
# is symlinks back into the real ~/.gemini — bin, brain, installation_id. Inside
# a box those point at nothing unless the original comes too, and agy refuses to
# start at all: "CLI failed to start - open .../installation_id".
BOX_HOME = ("~/.gemini",)
BOX_SETTINGS = ("~/.gemini/settings.json", "~/.gemini/config/mcp_config.json")

# agy has no config-dir variable of its own — it goes to $HOME/.gemini and that
# is that. So the profile IS a home directory, handed to the child through HOME,
# and mirrored from the real one (see layout.mirror) so it differs in exactly
# one file: the token. PROFILE_ENV is ours, not agy's: HOME is always set, so it
# cannot double as the "profile override" variable the way CODEX_HOME does.
HOME_ENV = "HOME"
PROFILE_ENV = "BROKER_AGY_HOME"
CANONICAL_HOME = os.path.expanduser("~")
PROFILE_BASE = os.path.expanduser("~/.agy")
AUTH_NAME = os.path.join(".gemini", "antigravity-cli", "antigravity-oauth-token")

# Where this harness declares its MCP servers. Its own file, not beside the
# settings: without this a box bridged nothing for agy at all, and the agent
# inside simply reported that agentbus was not there.
MCP_CONFIG = ("~/.gemini/config/mcp_config.json", "json", "mcpServers")

# Conversations, one database each, in the real home rather than the profile —
# the profile only differs in the token. Used to print a resume line that keeps
# the box (see box/run.py).
SESSION_GLOB = "%(home)s/.gemini/antigravity-cli/conversations/*.db"
SESSION_RESUME = "--conversation %s"

# The MCP tokens do not go into a box at all.
#
# This harness does not merge that file: it writes what its own session knows
# and drops everything else. A box has no keyring and no browser, so it knows
# nothing and writes an empty object — and the logins on this machine are gone.
# Four times in one afternoon, reported each time as "Unauthorized [Auth
# Needed]", which reads as an expired token rather than as a file emptied by a
# program next door.
#
# Giving each box its own copy was tried and is worse in the way that matters:
# the copy cannot be refreshed from outside, so it rots, and a box that
# authenticates for itself has to do it again in every other box. There are a
# dozen of them.
#
# So: one place holds these, and it is this machine. A box gets its own empty
# one, may ruin it as it likes, and the logins here survive. What a box loses
# is the http servers that need OAuth — ntk and joppa — which is a real loss,
# and a smaller one than losing the login itself.
BOX_BLANK = ((os.path.join("~", ".gemini", "antigravity-cli", "mcp_oauth_tokens.json"), "{}"),)

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
    "~/.gemini/GEMINI.md",
    "~/.gemini/skills",
)

# Ways of naming a conversation that already exists: the id is the person's
# then, not ours to guess. This harness keeps every conversation it has ever
# had in one directory — five hundred of them here, with nothing separating
# projects — so guessing is the last resort, not the first.
# Its own word for "show me the conversations".
SESSION_PICK = "--continue"

SESSION_PICKERS = ("--conversation", "--continue")
MIRROR_HOME = True

# The installer drops agy in ~/.local/bin; container images put it on the system
# path. Neither is a launcher script, so there is no vendored-binary dance here.
# The stash comes first: agy is not a launcher pointing elsewhere, it IS the
# 178 MB program that lives at ~/.local/bin/agy — so `broker agy install` moves it
# here before the shim takes the name (see lib/shim.js).
REAL_BINS = (
    os.path.join(config.REAL_DIR, "agy"),
    "/usr/local/lib/broker/real/agy",
    os.path.expanduser("~/.local/bin/agy"),
    "/usr/local/bin/agy",
    "/usr/bin/agy",
)
REAL_BIN = REAL_BINS[0]

# Subcommands about the installation, and purely local questions: neither should
# pick an account, and neither should depend on the broker being reachable.
# There is no `login`/`logout` here — agy starts its OAuth flow by itself as soon
# as it finds no token, which is exactly what `broker-agy auth <name>` uses.
PASSTHROUGH = ("install", "update", "changelog", "help", "--help", "-h", "--version", "-V")


# The engine reads the provider module: these names have to be attributes of it,
# wherever their code lives.
usage_request = agy_usage.usage_request
read_usage = agy_usage.read_usage
check_eligibility = agy_usage.check_eligibility


def route_refresh(env, cfg, account, auth):
    """Nothing to route: Google does not rotate this refresh token.

    codex needed the override because its refresh token is single-use — a second
    holder refreshing it kills the grant. Google hands back the same refresh
    token every time and tolerates concurrent refreshes, so agy renewing its own
    access token in the profile costs nothing and races with nobody. The broker
    still owns the account: it is what hands the token out in the first place.
    """
    return


def harness_env(env):
    """Keep agy's own updater from replacing the binary the shim points at.

    A self-update drops a fresh agy over ~/.local/bin/agy — which is where the
    shim lives. The install still looks fine and the next bare `agy` quietly
    stops going through the broker, running instead on whatever token happens to
    be in the real home. Updating stays a deliberate act: `broker-agy update`.
    """
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"


def login_env(env):
    return env


def login_cmd(_device):
    """Start agy with no arguments, which is how it signs you in.

    There is no `login` subcommand. Subcommands that merely need credentials do
    NOT start the flow either — `agy models` answers "Please sign in to view
    available models. Launch the CLI without arguments to sign in." So the login
    is the bare TUI: it prints a Google URL, takes the pasted code, and from then
    on the profile has its token. Quit it (Ctrl-C) once signed in.
    """
    return [BIN]


def read_local_auth(home):
    """The token file agy wrote after an interactive login, as the broker wants it."""
    try:
        with open(os.path.join(home, AUTH_NAME)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# Kept so the seed path can report a real expiry rather than guessing.
def expiry_epoch(auth):
    token = (auth or {}).get("token") or {}
    seconds = agy_usage._reset_seconds(token.get("expiry"))
    if seconds is None:
        return 0
    return int(calendar.timegm(datetime.datetime.utcnow().utctimetuple()) + seconds) * 1000
