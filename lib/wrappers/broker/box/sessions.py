"""Which session a run is, and how to come back to it.

Split out of run.py. Everything here answers one of two questions: what id this
run will have or had, and what to print so the person can reopen it. The box
machinery around it does not need to know how any of that is worked out.
"""

import glob as globmodule
import json
import os
import re
import shlex
import stat
import subprocess
import time
import uuid

from .. import config
from .paths import expand, home_dir


SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
def _pin_session(provider, argv):
    """Decide this run's session id before it starts, if the harness lets us.

    Otherwise the box has to work it out afterwards by reading the newest
    session file in the project's directory — which is right only while one
    harness writes there. Every box open on the same project shares that
    directory, so on a busy machine the newest file is somebody else's window,
    and the resume line the box prints leads into a stranger's conversation.
    The id is unguessable but not unknowable: harnesses accept it as an
    argument, so it is chosen here and simply known.

    Returns the id and the arguments to run with. When the person is already
    naming a session — resuming, continuing, picking one from a list — the id
    is theirs: it is read from what they typed, or left unknown when only a
    picker can answer, and nothing is added.
    """
    flag = getattr(provider, "SESSION_ID_FLAG", None)
    if not flag:
        return None, argv
    pickers = getattr(provider, "SESSION_PICKERS", ())
    for index, arg in enumerate(argv):
        key, _, inline = arg.partition("=")
        if key not in pickers:
            continue
        value = inline if inline else (argv[index + 1] if index + 1 < len(argv) else "")
        # A picker with nothing after it, or followed by the next flag: the
        # session is chosen inside the harness, out of our sight.
        return (value if SESSION_ID.match(value) else None), argv
    chosen = str(uuid.uuid4())
    return chosen, [flag[0], flag[1] % chosen, *argv]
def _created(path):
    """When this file came into being, where the filesystem records it."""
    stat = os.stat(path)
    return getattr(stat, "st_birthtime", stat.st_ctime)
def _session_id(path):
    """The id a harness gave this session, however it spells the filename."""
    stem = os.path.splitext(os.path.basename(path))[0]
    # Some name the file after the session; others prefix it with a timestamp
    # and leave the id at the end.
    match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", stem)
    return match.group(0) if match else stem
def _session_root(provider, config_dir):
    """Where this harness keeps its sessions, with links resolved."""
    pattern = getattr(provider, "SESSION_GLOB", "") or ""
    head = pattern.split("*", 1)[0] % {
        "config": config_dir, "home": home_dir(), "key": "",
    }
    return os.path.realpath(head)
def _sessions_are_shared(provider, env=None):
    """Whether a session can be reopened without naming the account.

    The account only matters when sessions live INSIDE the profile: the broker
    moves to another account when one runs out of room, and an id recorded
    under the first is then not found at all. When every profile reaches one
    pile — a link, or simply the same directory — naming the account adds
    nothing, and the broker choosing an account for itself is the point of it.
    """
    home_env = getattr(provider, "HOME_ENV", None)
    config = (env or {}).get(home_env) if home_env and home_env != "HOME" else None
    if not config:
        return True
    canonical = expand(getattr(provider, "CANONICAL_HOME", "~"))
    return _session_root(provider, expand(config)) == _session_root(provider, canonical)
def _session_path(provider, session, workdir, env=None):
    """The file this session was written to, or None if it cannot be placed."""
    pattern = getattr(provider, "SESSION_GLOB", None)
    if not (pattern and session):
        return None
    import glob as globmodule

    home_env = getattr(provider, "HOME_ENV", None)
    config = (env or {}).get(home_env) if home_env and home_env != "HOME" else None
    fields = {
        "key": workdir.replace(os.sep, "-"),
        "home": home_dir(),
        "config": config or expand(getattr(provider, "CANONICAL_HOME", "~")),
    }
    for path in globmodule.glob(pattern % fields):
        if _session_id(path) == session:
            return path
    return None
def _exit_note(provider, session, workdir, env=None):
    """Why the harness stopped, said again where it will still be readable.

    A harness that draws a full-screen interface does it on the terminal's
    alternate buffer: when it exits, the old screen comes back and everything
    it had shown goes with it. Someone whose account ran out mid-run is left
    facing a bare prompt, with no hint that anything was said at all — the
    message was there, for as long as the program was.
    """
    read = getattr(provider, "session_error", None)
    if not read:
        return None
    path = _session_path(provider, session, workdir, env)
    if not path:
        return None
    try:
        return read(path)
    except Exception:  # never let a note about an error become an error
        return None
LOG = "sessions.log"
def _session_from_argv(provider, argv):
    """The session named on the command line, when one was.

    Resuming and then typing nothing leaves the harness with nothing to save,
    so it names no session on the way out — correctly, it started none. But the
    id is right there in what was typed, and coming back to it is exactly what
    the person was in the middle of doing.
    """
    pickers = set(getattr(provider, "SESSION_PICKERS", ()) or ())
    pickers.add(getattr(provider, "SESSION_PICK", "resume"))
    wanted = False
    for word in argv or ():
        if wanted and _id_shape(provider).fullmatch(word.encode()):
            return word
        wanted = word in pickers
    return None
# How a session id looks. Most harnesses use a uuid; opencode spells its own
# "ses_" and then letters and digits, so the shape is asked of the provider
# rather than assumed — an id that does not match is an id that is never found,
# and the person is told their session has no name.
SAID_ID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
def _id_shape(provider):
    own = getattr(provider, "SESSION_ID_RE", None)
    if not own:
        return SAID_ID
    return re.compile(own.encode() if isinstance(own, str) else own)
def _session_it_named(printed, pattern=None, provider=None):
    """The session id the harness itself printed, if it did.

    Its own resume line is what is wanted, so that is looked for first; a bare
    id anywhere in the last screen is the fallback. The LAST one: a screen can
    carry older ids in the scrollback of what it was doing.
    """
    if not printed:
        return None
    text = re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(\x07|\x1b\\)", b"", printed)
    if pattern:
        named = re.findall(pattern.encode() if isinstance(pattern, str) else pattern, text)
        if named:
            return named[-1].decode()
    found = _id_shape(provider).findall(text) if provider else SAID_ID.findall(text)
    return found[-1].decode() if found else None
def _remember(provider, name, workdir, session, account, status):
    """Write down what was just run, so the way back survives anything.

    The line a box prints on its way out is the only place the session id
    appears — and it is printed by a program that has just been interrupted.
    Ctrl-C, a killed container, a terminal closed by accident: the run ends,
    the id goes with it, and what is left is a file among fourteen thousand
    whose name nobody knows.

    So it is also written here, one line per run, every run. Nothing clever:
    the point is that it is on disk before anyone needs it.
    """
    line = "%s\t%s\t%s\t%s\t%s\texit %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), provider.NAME, name or "-",
        workdir, session or "-", status)
    path = os.path.join(config.CONFIG_DIR, LOG)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a") as handle:
            handle.write(line)
    except OSError:
        pass  # a missing note is not a reason to fail the run
def _resume_hint(provider, name, workdir, env=None, since=0, account=None, session=None):
    """What to type to come back INTO this box, on the same account.

    The harness prints its own resume line as it exits, and that line is missing
    the box: run it as printed and the session reopens on the host, in a
    different world, which is not obvious until something behaves oddly.

    The account is named only when it would otherwise be lost: a harness that
    files its sessions INSIDE the per-account profile records an id the next
    account cannot find ("no rollout found for thread id"). When the profiles
    all reach one pile of sessions — which is the normal arrangement — the
    broker picks an account by itself and the line stays clean.
    """
    form = getattr(provider, "SESSION_RESUME", "--resume %s")
    if not session:
        # It did not say — killed before it could, most likely. The way back is
        # the harness's own picker, which knows; an id made up out here would
        # look exactly like an answer and be a stranger's conversation.
        return ("\nThis run did not name its session. To come back into this box:"
                "\n  %s %s --box %s\n"
                % (provider.BIN, getattr(provider, "SESSION_PICK", "resume"), name))
    if isinstance(session, (list, tuple)):
        # More than one was written. Every id, newest first — a guess would be
        # worse than a list, and no list at all is worst of them all: the only
        # other copy is on a screen the harness has already wiped.
        pin = ""
        if (account and getattr(provider, "CREDENTIALS", "file") != "env"
                and not _sessions_are_shared(provider, env)):
            pin = "%s_ACCOUNT=%s " % (provider.NAME.upper(), account)
        lines = ["\nSessions written by this run, newest first:"]
        lines += ["  %s%s %s --box %s" % (pin, provider.BIN, form % one, name)
                  for one in session]
        return "\n".join(lines) + "\n"
    resume = form % (session or "<the id printed above>")
    pin = ""
    if (account and getattr(provider, "CREDENTIALS", "file") != "env"
            and not _sessions_are_shared(provider, env)):
        pin = "%s_ACCOUNT=%s " % (provider.NAME.upper(), account)
    # The box goes last, after the id, so the line differs from the one the
    # harness printed above it only by a suffix: type that suffix onto the end
    # of what you already have, or delete it to go back to the host.
    return "\nResume it in this box with:\n  %s%s %s --box %s\n" % (pin, provider.BIN, resume, name)
def _session_from_store(provider, name, since):
    """Ask the harness which session it just wrote, when there are no files.

    One database and no session files means nothing on disk changes name when a
    conversation happens, so the only way to know what to fold back is to ask —
    in the box's own copy, which by now has no writer left.
    """
    shell = getattr(provider, "BOX_SESSION_SHELL", None)
    store = _private_store(provider, name) if shell else None
    if not store:
        return None
    from ..run import real_bin

    binary = real_bin(provider)
    if not binary:
        return None
    try:
        found = subprocess.run(
            ["/bin/sh", "-c", shell % {"bin": shlex.quote(binary),
                                       "store_parent": shlex.quote(os.path.dirname(store))}],
            capture_output=True, text=True, timeout=60)
        rows = json.loads(found.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    # Milliseconds there, seconds here.
    touched = [r for r in rows if isinstance(r, dict)
               and (r.get("updated") or 0) / 1000.0 >= since]
    if not touched:
        return None
    return max(touched, key=lambda r: r.get("updated") or 0).get("id")
def _private_store(provider, name):
    """The directory holding this box's own copy of the harness's databases."""
    if not name:
        return None
    root = os.path.join(config.CONFIG_DIR, "box", "private",
                        name.replace("/", "_"), provider.NAME)
    return root if os.path.isdir(root) else None
