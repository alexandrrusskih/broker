"""The file that defines boxes, and pulling `--box` out of a command line."""

import json
import os

from .. import config
from ..out import die

FLAG = "--box"
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
    """Every box defined on this machine, or {} if the file is not there."""
    return {name: box for name, box in _raw().items() if name != MACHINES}


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
    return _take(argv, FLAG, lambda: "%s needs a box name — one of: %s" % (
        FLAG, ", ".join(sorted(profiles())) or "none defined"))


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


