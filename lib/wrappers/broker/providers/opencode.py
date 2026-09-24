"""opencode: a harness the broker only puts in a box.

Nothing here logs in, picks an account or refreshes a token. opencode carries
its own subscription and its own login, and taking that over would mean holding
credentials we were never asked to hold. So this provider says one thing: what
this harness needs in order to run inside a box, and what must not be shared
with the copy outside it.
"""

import os

from .. import config

NAME = "opencode"
BIN = "opencode"
CMD = "broker-oc"

# The broker holds no credentials for this one, and the run path must not try:
# no account is chosen, nothing is refreshed, the harness logs in as it always
# did. `--box` is the only thing we add.
CREDENTIALS = None

HOME_ENV = "HOME"
CANONICAL_HOME = os.path.expanduser("~")

# Its servers are declared in JSON with comments, under "mcp", and each entry
# spells a command as a LIST rather than a string:
#   "codebase-memory": { "type": "local", "command": ["/path/to/server"],
#                        "environment": {...} }
MCP_CONFIG = ("~/.config/opencode/opencode.jsonc", "jsonc", "mcp")

# One SQLite database for every session it has ever had — a gigabyte of it
# here. Shared across the container boundary it would tear: the journal lives
# beside the database, and two writers on either side of that boundary is
# exactly how we lost a day of codex history. A box gets its own clone.
BOX_PRIVATE = (".local/share/opencode/opencode.db",)

# It writes beside that database — caches, checked-out repositories, logs — and
# the directory holding it is created by the container as root when the clone
# above is mounted. Claim it first, so it belongs to the user who runs here.
BOX_WRITABLE = ("~/.local", "~/.local/share/opencode")

# Folded back on the way out, and this is the only safe way to do it.
#
# The obvious way — copy the database over — cannot work: it is one file for
# every session there has ever been, so copying the box's copy outward would
# throw away everything done outside meanwhile, and merging two SQLite files by
# hand means guessing at someone else's schema.
#
# So the harness is asked to do it, in its own words: export the one session
# out of the box's copy, import it into the real one. Both commands are the
# harness's own, both operate on one session, and the import is the only writer
# touching the database outside.
#
# It runs AFTER the container is gone. That is what makes it safe: the box's
# copy has no writer left, so there is nothing to race with — which is exactly
# what went wrong the last time this was attempted, with a different harness,
# while its box was still running.
BOX_SYNC = ("shell",)  # the steps live in BOX_SYNC_SHELL below

# The export above writes JSON to stdout and the import reads a file, so the
# two are joined by one: a temporary file, and the store to read from. Spelled
# here rather than in the engine, because only this harness works this way.
BOX_SYNC_SHELL = (
    'set -e; f="$(mktemp -t opencode-session)"; trap \'rm -f "$f"\' EXIT; '
    'XDG_DATA_HOME=%(store_parent)s %(bin)s export %(session)s > "$f"; '
    '%(bin)s import "$f"'
)

# Where the real harness is, once the shim has taken its name. The shim itself
# lives in ~/.local/bin, so that one is listed last: finding it first would send
# the launcher back to itself.
REAL_BINS = (
    os.path.join(config.REAL_DIR, "opencode"),
    "/usr/local/lib/broker/real/opencode",
    "/opt/homebrew/bin/opencode",
    "/usr/local/bin/opencode",
    os.path.expanduser("~/.local/bin/opencode"),
)
REAL_BIN = REAL_BINS[0]

# Which session this run was. There are no session FILES to look at — it is all
# in the database — so the harness is asked, in its own copy, for the sessions
# it touched while the box ran. Anything older belongs to an earlier run and is
# already folded back.
BOX_SESSION_SHELL = (
    'XDG_DATA_HOME=%(store_parent)s %(bin)s session list --format json'
)

SESSION_RESUME = "-s %s"
# Continue, which is where its list lives.
SESSION_PICK = "-c"

SESSION_PICKERS = ("-s", "--session", "-c", "--continue")

# Its own installation commands, which must reach the harness untouched.
PASSTHROUGH = ("auth", "upgrade", "models", "serve", "export", "import",
               "session", "help", "--help", "-h", "--version", "-v")
