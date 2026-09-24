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
def session_error(path):
    """The last failure this session recorded, or None if it ended cleanly."""
    import json

    try:
        with open(path, "rb") as handle:
            # The tail only: these run to tens of megabytes, and what is wanted
            # is the final turn.
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 262144))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            payload = (json.loads(line) or {}).get("payload") or {}
        except ValueError:
            continue
        if payload.get("type") != "task_complete":
            continue
        # A turn that ended normally carries no error at all, and once one has
        # been found there is no point reading further back: older turns are
        # not what the person is standing in front of.
        message = ((payload.get("error") or {}).get("message") or "").strip()
        return message or None
    return None

# Where the databases go: one set per WINDOW, rather than one set for the
# machine.
#
# They are SQLite, and this harness has no BUSY retry: the log writer flushes
# every two seconds holding the write lock, and a second process arriving in
# that moment either hangs or is told the database is locked. Worse, a start
# is REFUSED outright when anything holds a write lock on the log database —
# the telemetry file gates the boot. Eight windows open on this machine and a
# day of it ends the way this one did: "database disk image is malformed", a
# state file that is no longer a database at all, and every session in the
# picker gone.
#
# It is not ours to fix and it is known upstream (openai/codex #20213, #35555,
# #30105, #44772); what everyone is told to do is give each instance its own.
# CODEX_HOME alone does not: the harness symlinks every database back into the
# canonical ~/.codex whatever that variable says — measured here, by putting
# real empty files there and watching one run replace them with links again.
# CODEX_SQLITE_HOME is the one it honours.
#
# What stays shared is what matters: the sessions themselves, the settings, the
# accounts. The databases are an index over those files — a fresh one filled
# itself from all 13,774 rollouts on first start, so nothing is lost by a
# window starting with none.
#
# On the big disk, not in the home: a filled index is some 600 MB, and there
# are a dozen windows.
SQLITE_ENV = "CODEX_SQLITE_HOME"
SQLITE_BASE = "/Volumes/hdd/broker-box/codex-db"


def harness_env(env):
    """Point this window's harness at its own databases."""
    # The stale links go whatever else happens here: they are what stops a box
    # from starting at all.
    _unlink_shared_databases(env.get(HOME_ENV))
    # Not forced: someone who set it meant it.
    if env.get(SQLITE_ENV):
        return
    from ..box.paths import window_key

    base = SQLITE_BASE if os.path.isdir(os.path.dirname(SQLITE_BASE)) else \
        os.path.expanduser("~/.cache/broker/codex-db")
    own = os.path.join(base, window_key())
    fresh = not os.path.isdir(own)
    try:
        os.makedirs(own, mode=0o700, exist_ok=True)
    except OSError:
        return  # the shared ones are worse, but they are better than no start
    if fresh:
        _seed_databases(own)
    env[SQLITE_ENV] = own


def _unlink_shared_databases(home):
    """Take the links to the machine-wide databases out of this profile.

    The harness puts them there itself, every start: whatever CODEX_HOME says,
    each database in it becomes a link back into the canonical directory. With
    CODEX_SQLITE_HOME set the real files go elsewhere and the links are merely
    stale — except they are not merely anything. One of them is the telemetry
    database, and a start is REFUSED while another process holds a write lock
    on it. On a machine with seven of these open, a box would sit at "model:
    loading" until something let go, which is to say it would not start at all
    — while a plain run on the host, already holding the file, started fine.
    That reads as "the box is broken".

    Only links, and only inside a profile: a real database here is someone's
    data, and the canonical directory is not ours to prune.
    """
    import glob as globmodule

    if not home:
        return
    home = os.path.realpath(os.path.expanduser(home))
    if home == os.path.realpath(CANONICAL_HOME):
        return
    for path in globmodule.glob(os.path.join(home, "*.sqlite*")):
        if os.path.islink(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _seed_databases(own):
    """Start a window's databases from the ones already built.

    Left to itself a new set is filled from every rollout on disk — fourteen
    thousand files here — and the window sits on "Waiting for startup" for
    minutes while it happens, once per window. So it starts from a copy
    instead, and only what is missing gets read.

    The canonical directory is the source: now that every window has its own,
    nothing writes there any more, which makes it the one copy that is never
    half-written. A clone on APFS costs no space and no time — 600 MB in five
    milliseconds, measured — and where the filesystem cannot clone, this is a
    plain copy, still cheaper than reading every session.

    Only the databases themselves: -wal and -shm belong to whoever had the file
    open, and a journal that does not match its database is exactly how a fresh
    start turns into "database disk image is malformed".
    """
    import glob as globmodule
    import shutil
    import subprocess

    for source in sorted(globmodule.glob(os.path.join(CANONICAL_HOME, "*.sqlite"))):
        target = os.path.join(own, os.path.basename(source))
        if os.path.exists(target):
            continue
        try:
            if subprocess.run(["cp", "-c", source, target],
                              capture_output=True).returncode:
                shutil.copy2(source, target)
        except OSError:
            return  # an empty set still works, it is only slower


def session_of_run(env, since):
    """The id of the conversation THIS run had, from its own log database.

    Every window on this machine files its sessions in one directory, so
    "the newest file" names whoever typed last, not this box — which is why
    the box used to print a list, or nothing. The log database is different:
    since each window has its own, the last thread mentioned in it is this
    window's, with nobody else writing there to confuse it.

    `since` is when the run started: an id from before it belongs to an
    earlier conversation in the same window, and naming that one would be the
    same wrong answer in a smarter disguise.
    """
    import sqlite3

    base = (env or {}).get(SQLITE_ENV)
    if not base:
        return None
    path = os.path.join(base, "logs_2.sqlite")
    if not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        row = db.execute(
            "select substr(feedback_log_body,"
            "  instr(feedback_log_body, 'thread_id=') + 10, 36) "
            "from logs where feedback_log_body like '%thread_id=%' and ts >= ? "
            "order by id desc limit 1", (int(since),)).fetchone()
        db.close()
    except Exception:
        return None
    found = (row or [None])[0]
    return found if found and len(found) == 36 else None
