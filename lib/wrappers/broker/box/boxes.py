"""The file that defines boxes, and pulling `--box` out of a command line."""

import hashlib
import json
import os
import re
import subprocess

from .. import config
from ..out import die
from .paths import expand, home_dir

FLAG = "--box"
# `--box .` is the project you are standing in. It needs no entry of its own:
# a box that already lists the project is that box, and any other project gets
# one made on the spot from the template below. Nothing is written to the file.
HERE = "."
# What every box starts from, named so that no folder would be called it:
#   "__defaults": { "env": {"GIT_AUTHOR_NAME": "..."}, "mcp": {...} }
# A box's own value wins; objects merge key by key, lists add to the template's.
DEFAULTS = "__defaults"
# Which machine the box runs on. The harness, the container and the project all
# live over there; what stays here is the terminal you are typing into.
REMOTE_FLAG = "--remote"
# Machines are described beside the boxes, under a name that is not a box:
#   "machines": { "windows": { "ssh": "user@host", "paths": {"/here": "/there"} } }
MACHINES = "machines"
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
    """Every box defined on this machine, each on top of the template."""
    raw = _raw()
    base = _defaults(raw)
    return {name: _merged(base, box) for name, box in raw.items()
            if name not in (MACHINES, DEFAULTS)}


def _defaults(raw):
    base = raw.get(DEFAULTS) or {}
    if not isinstance(base, dict):
        die("%s in %s must be an object" % (DEFAULTS, PATH))
    # A place to write is what tells one box from the next: in the template it
    # would hand every project on the machine the same one. A Dockerfile is
    # built per box, under that box's name, and a box made on the spot has no
    # image built for it — "image" names a tag that exists, and is fine here.
    for key in ("rw", "dockerfile"):
        if key in base:
            die("%s in %s cannot say \"%s\": it would apply to every box" % (DEFAULTS, PATH, key))
    return base


def _merged(base, own):
    if not isinstance(own, dict):
        return own
    out = dict(base)
    for key, value in own.items():
        have = out.get(key)
        if isinstance(have, dict) and isinstance(value, dict):
            out[key] = {**have, **value}
        elif isinstance(have, list) and isinstance(value, list):
            out[key] = have + [item for item in value if item not in have]
        else:
            out[key] = value
    return out


def resolve(name, cwd=None):
    """The box to run: its name, its settings, and what to type to come back.

    The name is what the box's own state is kept under. For `.` it is made from
    the project — its folder and a hash of its path, so two projects that are
    both called "api" do not share copies — and is never typed: the way back
    into such a box is `--box .` again, from the same project.
    """
    defined = profiles()
    if name != HERE:
        if name not in defined:
            die("no box called '%s' in %s%s" % (
                name, PATH,
                (" — defined: " + ", ".join(sorted(defined))) if defined else " (the file does not exist yet)"))
        return name, defined[name], name
    cwd = os.path.realpath(cwd or os.getcwd())
    owner = _owning_box(defined, cwd)
    if owner:
        return owner, defined[owner], owner
    paths = _project(cwd)
    slug = re.sub(r"[^a-z0-9_.-]+", "-", os.path.basename(paths[0]).lower()).strip("-.") or "project"
    digest = hashlib.sha1(paths[0].encode()).hexdigest()[:6]
    return "%s-%s" % (slug, digest), _merged(_defaults(_raw()), {"rw": paths}), HERE


def _owning_box(defined, cwd):
    """The named box whose "rw" holds this directory — the closest one."""
    best, length = None, -1
    for name, box in defined.items():
        for entry in (box.get("rw") or []) if isinstance(box, dict) else []:
            if not isinstance(entry, str):
                continue  # {source, target}: a private directory, not a project
            path = os.path.realpath(expand(entry))
            if (cwd == path or cwd.startswith(path.rstrip(os.sep) + os.sep)) and len(path) > length:
                best, length = name, len(path)
    return best


def _project(cwd):
    """What `.` mounts: the repository around here, or this directory.

    A worktree keeps its history in the main repository's .git, which lies
    outside it; without that, git inside the box finds no repository at all.
    """
    found = []
    try:
        git = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute",
             "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, timeout=10)
        if git.returncode == 0:
            found = [os.path.realpath(line) for line in git.stdout.splitlines() if line]
    except (OSError, subprocess.SubprocessError):
        pass
    home = os.path.realpath(home_dir())

    def holds_home(path):
        return path == home or (home + os.sep).startswith(path.rstrip(os.sep) + os.sep)

    # A home directory kept in git makes every folder under it one repository;
    # the project is then the folder you are in, not the whole home.
    root = found[0] if found and not holds_home(found[0]) else cwd
    if holds_home(root):
        die("`%s .` here would give the box %s, and your whole home with it — cd into a project first"
            % (FLAG, root))
    paths = [root]
    common = found[1] if len(found) > 1 else None
    if common and common != root and not common.startswith(root + os.sep) and not holds_home(common):
        paths.append(common)
    return paths


def _raw():
    """The file as written, boxes and machines together."""
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


def _take(argv, flag, missing):
    """Pull `<flag> <value>` out of the arguments, leaving the rest untouched.

    Everything after a bare `--` belongs to the harness and is never inspected:
    a prompt mentioning --box is a prompt, not a flag.
    """
    rest, value = [], None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest.extend(argv[i:])
            break
        if arg == flag:
            if i + 1 >= len(argv):
                die(missing())
            value, i = argv[i + 1], i + 2
            continue
        if arg.startswith(flag + "="):
            value, i = arg.split("=", 1)[1], i + 1
            continue
        rest.append(arg)
        i += 1
    return value, rest


def take_flag(argv):
    return _take(argv, FLAG, lambda: "%s needs a box name — one of: %s, or %s for the project you are in" % (
        FLAG, ", ".join(sorted(profiles())) or "none defined", HERE))


def machines():
    """The machines a box may be sent to, by name."""
    found = _raw().get(MACHINES)
    return found if isinstance(found, dict) else {}


def take_remote(argv):
    """Pull `--remote <machine>` out, and say what it resolves to."""
    name, rest = _take(argv, REMOTE_FLAG, lambda: "%s needs a machine name — one of: %s" % (
        REMOTE_FLAG, ", ".join(sorted(machines())) or "none defined in " + PATH))
    if name is None:
        return None, None, rest
    known = machines()
    if name not in known:
        die("no machine called '%s' in %s%s" % (name, PATH,
            (" — defined: " + ", ".join(sorted(known))) if known else " (add a \"machines\" section)"))
    return name, known[name], rest


