"""Running a harness inside a container, with the project and nothing else.

The point is not sandboxing for its own sake: it is being able to hand a harness
every permission it asks for — write files, run commands, install things — while
the blast radius stays the directories you named. Inside the box it is root over
its own world; outside it can reach the project, its own settings, and nothing
more.

Two things make the difference between "it runs" and "it is usable":

  * paths match the host exactly. A project at ~/Projects/foo is mounted at
    ~/Projects/foo, and $HOME is the host's $HOME. Harnesses key their session
    history off the absolute path of the working directory, so a project moved
    to /workspace would lose every past session and every --resume.
  * the harness's own directory comes along. ~/.claude for claude, ~/.codex for
    codex — settings, MCP servers, agents, history. Selecting parts of it would
    silently drop whatever the next release adds, so it travels whole, minus the
    credentials file: the token arrives from the broker, and a copy on disk
    inside the box is a copy that can leak out of it.
"""

import json
import os
import re
import shutil
import sys

from . import config
from .out import die, warn

FLAG = "--box"
PATH = os.path.join(config.CONFIG_DIR, "boxes.json")

# Kept out of the image so it is yours to edit, and never rewritten by us.
EXAMPLE = """{
  // Every box is a name and the directories it may touch. Paths are mounted at
  // the SAME path inside, so sessions and --resume keep working.
  "work": {
    "rw": ["~/Projects/example"],
    "ro": ["~/Projects/reference"]
  }
}
"""


def _strip_comments(text):
    """JSON with // and /* */ comments — a config a human edits deserves them."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and (text[j] != '"' or text[j - 1] == "\\"):
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                break
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def profiles():
    """Every box defined on this machine, or {} if the file is not there."""
    try:
        with open(PATH) as fh:
            raw = json.loads(_strip_comments(fh.read()))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        die("unreadable %s: %s" % (PATH, exc))
    if not isinstance(raw, dict):
        die("%s must be an object of box names" % PATH)
    return raw


def take_flag(argv):
    """Pull `--box <name>` out of the arguments, leaving the rest untouched.

    Everything after a bare `--` belongs to the harness and is never inspected:
    a prompt mentioning --box is a prompt, not a flag.
    """
    rest, name = [], None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest.extend(argv[i:])
            break
        if arg == FLAG:
            if i + 1 >= len(argv):
                die("%s needs a box name — one of: %s" % (FLAG, ", ".join(sorted(profiles())) or "none defined"))
            name, i = argv[i + 1], i + 2
            continue
        if arg.startswith(FLAG + "="):
            name, i = arg.split("=", 1)[1], i + 1
            continue
        rest.append(arg)
        i += 1
    return name, rest


def _paths(profile, key):
    for entry in profile.get(key) or []:
        yield os.path.abspath(os.path.expanduser(entry))


def _mount(host, mode="rw"):
    return ["--mount", "type=bind,source=%s,target=%s%s" % (host, host, ",readonly" if mode == "ro" else "")]


def _empty_file():
    """A file to cover a credentials path with, so the box cannot read one."""
    blank = os.path.join(config.CACHE_DIR, "box-empty")
    os.makedirs(os.path.dirname(blank), mode=0o700, exist_ok=True)
    if not os.path.exists(blank):
        with open(blank, "w"):
            pass
        os.chmod(blank, 0o600)
    return blank


def command(provider, name, profile, argv, env):
    """The full container command line for this run."""
    runtime = profile.get("runtime") or "docker"
    binary = shutil.which(runtime)
    if not binary:
        die("%s is not installed — the '%s' box asks for it" % (runtime, name))

    home = os.path.expanduser("~")
    image = profile.get("image") or "broker-box"

    cmd = [binary, "run", "--rm", "--init"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        cmd.append("-it")
    # The harness writes as you, not as root: files it creates in the project
    # stay yours, and nothing needs chown afterwards.
    cmd += ["--user", "%d:%d" % (os.getuid(), os.getgid())]
    cmd += ["-e", "HOME=%s" % home, "-e", "USER=%s" % (os.environ.get("USER") or "user")]
    # $HOME itself is a tmpfs owned by that uid. Without it the harness cannot
    # write to its own home: the container creates missing mount points as root,
    # and mounting the real home instead would hand the box everything in it.
    # The directories below land on top of this, so what is mounted survives and
    # what is not is discarded with the container.
    cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700" % (home, os.getuid(), os.getgid())]

    mounted = []
    # The harness's own directory: settings, MCP servers, agents, history.
    for entry in getattr(provider, "BOX_HOME", ()):
        host = os.path.expanduser(entry)
        if os.path.exists(host):
            cmd += _mount(host)
            mounted.append(host)
    # ...minus its credentials file. The token comes from the broker below.
    blank = None
    for entry in getattr(provider, "BOX_SECRETS", ()):
        host = os.path.expanduser(entry)
        if any(host.startswith(m + os.sep) for m in mounted):
            blank = blank or _empty_file()
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly" % (blank, host)]

    projects = list(_paths(profile, "rw"))
    for host in projects:
        if not os.path.isdir(host):
            die("the '%s' box lists %s, which does not exist" % (name, host))
        cmd += _mount(host)
    for host in _paths(profile, "ro"):
        if not os.path.isdir(host):
            die("the '%s' box lists %s, which does not exist" % (name, host))
        cmd += _mount(host, "ro")

    # Start where you started, when that is inside the box; otherwise in the
    # first writable project, so a bare `claude --box work` lands somewhere real.
    cwd = os.getcwd()
    inside = any(cwd == p or cwd.startswith(p + os.sep) for p in projects)
    cmd += ["-w", cwd if inside else (projects[0] if projects else home)]

    # A provider that reads its credentials from a FILE has them in a profile
    # directory; that directory comes in at its own path, and the variable
    # pointing at it comes with it.
    home_env = getattr(provider, "HOME_ENV", None)
    if getattr(provider, "CREDENTIALS", "file") != "env" and home_env and env.get(home_env):
        host = os.path.abspath(os.path.expanduser(env[home_env]))
        if os.path.isdir(host) and host not in mounted:
            cmd += _mount(host)
            cmd += ["-e", "%s=%s" % (home_env, host)]

    for key, value in sorted((profile.get("env") or {}).items()):
        cmd += ["-e", "%s=%s" % (key, value)]
    # The credentials for this run, and the marker that tells a harness spawning
    # itself inside the box that it is already brokered.
    carried = ["BROKER_ACTIVE"]
    if getattr(provider, "CREDENTIALS", "file") == "env":
        carried.insert(0, provider.ENV_NAME)
    for key in carried:
        if env.get(key):
            cmd += ["-e", "%s=%s" % (key, env[key])]

    cmd.append(image)
    cmd.append(provider.BIN)
    cmd += argv
    return cmd


def exec_box(provider, name, argv, env):
    """Become the container. Nothing runs on the host after this."""
    defined = profiles()
    if name not in defined:
        die("no box called '%s' in %s%s" % (
            name, PATH,
            (" — defined: " + ", ".join(sorted(defined))) if defined else " (the file does not exist yet)"))
    cmd = command(provider, name, defined[name], argv, env)
    try:
        os.execv(cmd[0], cmd)
    except OSError as exc:
        die("cannot start the '%s' box: %s" % (name, exc))
