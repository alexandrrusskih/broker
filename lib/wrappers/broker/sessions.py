"""Which account a harness session ran on, so resuming it lands there again.

The account is chosen per run, and nothing outside the broker remembers that
choice. A terminal multiplexer restoring its panes knows only "claude, session
<id>" and runs `claude --resume=<id>` — so every restored pane went back to the
default account, even the ones moved off it because it ran out of room. Worse,
a restore starts a couple of dozen panes in one second, their usage probes race
over one ~/.claude and come back empty, and an empty probe counts as room: the
default won every time, exhausted or not.

So the account is written down per session id at launch, and a resume starts
from that account instead of the default. It is a starting point, not a pin:
if the remembered account has no room left, selection still looks elsewhere.
"""

import json
import os
import tempfile
import time
import uuid

from . import config

# Sessions older than this are not resumed often enough to be worth keeping.
MAX_AGE = 90 * 86400
MAX_ENTRIES = 5000

# Options that already decide which session runs, or that start no session a
# later resume could name. A session id is only supplied when none is present.
_SESSION_OPTIONS = (
    "-r", "--resume", "-c", "--continue", "--session-id", "--fork-session",
    "--from-pr", "--cloud", "-p", "--print", "-h", "--help", "-v", "--version",
)


def _file(provider):
    return os.path.join(config.CACHE_DIR, "sessions-%s.json" % provider.NAME)


def _load(provider):
    try:
        with open(_file(provider)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(provider, data):
    path = _file(provider)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".sessions-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _option_value(argv, short, long_):
    for i, arg in enumerate(argv):
        if arg == "--":
            return None
        if arg.startswith(long_ + "="):
            return arg[len(long_) + 1:] or None
        if arg in (short, long_) and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return argv[i + 1]
    return None


def session_id(provider, argv):
    """The session this run continues or creates, if the arguments name one."""
    if not getattr(provider, "SESSION_IDS", False):
        return None
    return (
        _option_value(argv, "-r", "--resume")
        or _option_value(argv, None, "--session-id")
    )


def assign(provider, argv):
    """(session_id, argv) for a run that starts a new interactive session.

    The id has to be chosen here: the harness only invents one after the broker
    has already handed over, and a session nobody wrote down cannot be resumed
    on its own account. Anything that is not a plain new session — a resume, a
    one-shot print, a subcommand, a help screen — is left exactly as typed.
    """
    if not getattr(provider, "SESSION_IDS", False):
        return None, argv
    subcommands = getattr(provider, "SUBCOMMANDS", ())
    for arg in argv:
        if arg == "--":
            break
        name = arg.split("=", 1)[0]
        if name in _SESSION_OPTIONS or arg in subcommands:
            return None, argv
    sid = str(uuid.uuid4())
    return sid, ["--session-id", sid] + argv


def recall(provider, sid):
    if not sid:
        return None
    entry = _load(provider).get(sid)
    if isinstance(entry, dict):
        return entry.get("account") or None
    return None


def remember(provider, sid, account):
    """Write down the account a session runs on. Never fails the run."""
    if not sid or not account:
        return
    try:
        data = _load(provider)
        now = int(time.time())
        data[sid] = {"account": account, "at": now}
        fresh = {
            k: v for k, v in data.items()
            if isinstance(v, dict) and now - int(v.get("at") or 0) <= MAX_AGE
        }
        if len(fresh) > MAX_ENTRIES:
            keep = sorted(fresh, key=lambda k: fresh[k].get("at") or 0)[-MAX_ENTRIES:]
            fresh = {k: fresh[k] for k in keep}
        _save(provider, fresh)
    except OSError:
        pass  # forgetting a session costs one resume on the default, nothing more
