"""Run a shell line while holding one lock file.

Used for folding a session back into a harness's own history. Those steps leave
the session in a state no box may copy — archived, briefly — and the copying
happens in a different process, and often a different run. A file is the only
thing both can agree on.
"""

import fcntl
import os
import subprocess
import sys

if len(sys.argv) < 3:
    raise SystemExit("usage: holdlock.py <lockfile> <shell line>")

path, line = sys.argv[1], " ".join(sys.argv[2:])
try:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    handle = open(path, "a+")
    fcntl.flock(handle, fcntl.LOCK_EX)
except OSError:
    handle = None  # never fatal: without the lock this is what it was before

try:
    raise SystemExit(subprocess.run(["/bin/sh", "-c", line]).returncode)
finally:
    if handle is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
