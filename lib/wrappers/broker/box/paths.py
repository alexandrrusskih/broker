"""Where things are, and how they are mounted.

Every path a box deals with passes through here: what "~" means when a harness
has been handed a profile, which paths are mounted at which, and the account
file a container needs to know who you are.
"""

import os
import pwd
import subprocess

from .. import config
from ..out import die, warn

REAL_HOME_ENV = "BROKER_REAL_HOME"


def home_dir():
    """Your home — the one you had before a profile took over $HOME.

    A provider whose credentials live in a FILE gives the harness a profile
    directory as $HOME, so by the time a box is built "~" no longer means what
    it says. Expanding it then put ~/.gemini inside the profile itself: the real
    one was never mounted, every symlink the profile makes back into it dangled,
    and agy refused to start over a file that was there all along.

    $HOME still wins when nothing has been substituted — a container, a test or
    anything else that sets it means it.
    """
    return os.environ.get(REAL_HOME_ENV) or os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir


def expand(path):
    """expanduser(), but against the real home rather than the current $HOME."""
    path = str(path)
    if path == "~":
        return home_dir()
    if path.startswith("~" + os.sep):
        return os.path.join(home_dir(), path[2:])
    return os.path.expanduser(path)


# Inside a container "localhost" is the container. This is the host.
def _paths(profile, key):
    """Each path a box lists, as (what to mount, where it lands inside).

    Usually those are the same — a project keeps its own path, which is what
    makes session history and --resume work. They differ when a box needs its
    OWN copy of something the host also has: a directory a shared tool insists
    on writing to, where two boxes writing into one place would mix their state.
    Then the box names where it really is and where the tool expects it.
    """
    for entry in profile.get(key) or []:
        if isinstance(entry, dict):
            source = os.path.abspath(expand(entry.get("source") or ""))
            target = os.path.abspath(expand(entry.get("target") or source))
            if not entry.get("source"):
                die("a path in the box lists no source: %r" % (entry,))
            # Its own, so it has to exist before the box can start.
            if not os.path.exists(source):
                os.makedirs(source, mode=0o700, exist_ok=True)
            yield source, target
        else:
            path = os.path.abspath(expand(entry))
            yield path, path


def _passwd_file(runtime, image):
    """An /etc/passwd that knows who you are inside the box.

    The box runs as your own uid, which no image has an account for — and some
    tools refuse to start without one: ssh dies with "No user exists for uid
    501" before it reads a single option, which takes the Windows test offload
    with it. So the image's own passwd gets one line appended and is mounted
    back over itself. Built once and cached; the file it is built from changes
    about as often as the image is rebuilt.
    """
    cache = os.path.join(config.CONFIG_DIR, "box", "passwd-%s" % image.replace("/", "_").replace(":", "_"))
    if os.path.exists(cache):
        return cache
    try:
        base = subprocess.run([runtime, "run", "--rm", "--entrypoint", "cat", image, "/etc/passwd"],
                              capture_output=True, text=True, timeout=120)
        if base.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    user = os.environ.get("USER") or "user"
    line = "%s:x:%d:%d::%s:/bin/bash\n" % (user, os.getuid(), os.getgid(), home_dir())
    try:
        os.makedirs(os.path.dirname(cache), mode=0o700, exist_ok=True)
        tmp = cache + ".new"
        with open(tmp, "w") as fh:
            fh.write(base.stdout if base.stdout.endswith("\n") else base.stdout + "\n")
            fh.write(line)
        os.chmod(tmp, 0o644)
        os.replace(tmp, cache)
    except OSError:
        return None
    return cache


def _mount(host, mode="rw", target=None):
    """Mount a path at its own path — and at its physical one too, if they differ.

    ~/Projects/foo is often a symlink to /Volumes/.../foo. Inside the box only
    the name you gave would exist, and that breaks things that resolve symlinks
    on the host: a bridged MCP server answers with /Volumes/... paths, and the
    harness inside cannot open a single one of them. Mounting both costs one
    more bind of the same source.
    """
    flag = ",readonly" if mode == "ro" else ""
    target = target or host
    args = ["--mount", "type=bind,source=%s,target=%s%s" % (host, target, flag)]
    # Only when the path is kept as-is: a redirected mount is already somewhere
    # else on purpose, and its physical twin would land on top of the original.
    physical = os.path.realpath(host)
    if target == host and physical != host:
        args += ["--mount", "type=bind,source=%s,target=%s%s" % (physical, physical, flag)]
    return args


def _empty_file():
    """A file to cover a credentials path with, so the box cannot read one."""
    blank = os.path.join(config.CACHE_DIR, "box-empty")
    os.makedirs(os.path.dirname(blank), mode=0o700, exist_ok=True)
    if not os.path.exists(blank):
        with open(blank, "w"):
            pass
        os.chmod(blank, 0o600)
    return blank


