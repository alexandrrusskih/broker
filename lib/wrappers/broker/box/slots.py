"""How much of this machine's capacity for heavy work is free.

The ceiling is two heavy runs per MACHINE, not per box: thirteen boxes with two
each would be twenty-six, which is no ceiling at all. It is kept as lock files
in one directory every box mounts, and a run holds one while it works.

Nothing here takes a slot. It only looks, so it can be called from anywhere —
including by someone deciding what to start next.
"""

import fcntl
import os


def _slot_files(directory):
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.startswith("slot") and not name.endswith(".owner")
    )


def state(directory):
    """(free, total, [names of the busy ones]).

    A slot is busy when its lock cannot be taken. Taken and released at once:
    holding it here would make asking the question cost a slot.
    """
    try:
        files = _slot_files(directory)
    except OSError:
        return (0, 0, [])
    busy = []
    for path in files:
        try:
            handle = open(path, "a+")
        except OSError:
            busy.append(os.path.basename(path))
            continue
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            busy.append(os.path.basename(path))
        finally:
            handle.close()
    return (len(files) - len(busy), len(files), busy)


def owner(directory, slot):
    """Who said they were holding this one, if anyone left a note."""
    try:
        with open(os.path.join(directory, slot + ".owner")) as fh:
            return fh.read().strip()
    except OSError:
        return ""
