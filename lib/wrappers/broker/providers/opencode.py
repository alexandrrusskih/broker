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

# Nothing is folded back on the way out. A session written in the box stays in
# the box's own copy; the database outside is left as it was found.
BOX_SYNC = ()

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

SESSION_RESUME = "-s %s"
SESSION_PICKERS = ("-s", "--session", "-c", "--continue")

# Its own installation commands, which must reach the harness untouched.
PASSTHROUGH = ("auth", "upgrade", "models", "serve", "export", "import",
               "session", "help", "--help", "-h", "--version", "-v")
