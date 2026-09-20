"""The file that defines boxes, and pulling `--box` out of a command line."""

import json
import os

from .. import config
from ..out import die

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


