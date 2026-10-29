"""The terminal manager, reachable from inside a box.

The socket is not the obstacle. Measured, against the claim that stood in this
docstring before: Docker Desktop does carry herdr's unix socket into a
container, it arrives as a socket, and a process inside connects to it.

The CLIENT is the obstacle. herdr is a Mach-O arm64 executable, and a Linux
container has nothing to run it with. So a box holding that socket would have
nothing able to speak to it, and the alternative — teaching a stand-in herdr's
own wire protocol — buys a copy of somebody else's protocol to keep in step.

So the socket stays on the host and only the COMMAND crosses, which is the
shape the MCP bridge already uses. A box gets a stand-in at herdr's own path;
it sends the arguments it was called with, the host runs the real binary and
sends back what it printed. The listener, the secret and the state file are
box/bridge.py, shared with the browser door, which is the same door.

The host end is also where a box can be held to less than everything. Through
that socket `herdr agent read` shows any agent's screen, `send-keys` types
into any of them, and `pane process-info` prints command lines that carry the
fleet's tokens. A box listing `herdr: ["tab", "pane"]` gets those and nothing
else; one that lists nothing gets no stand-in at all.
"""

import os
import subprocess
import sys

from ..out import warn
from . import bridge

KIND = "herdr"
# What a box may be given. The first word of the command decides, which is how
# herdr groups them itself: `herdr tab list`, `herdr agent read`.
ALL = "*"


def allowed(argv, groups):
    """Whether a box that may use `groups` is allowed to run this command."""
    for word in argv:
        if word.startswith("-"):
            continue  # a flag before the subcommand decides nothing
        return ALL in groups or word in groups
    # A bare `herdr` opens the interactive terminal, which inside a box has
    # nothing to draw on and would simply hang. Refused even where everything
    # else is allowed.
    return False


def _run(argv, binary, groups):
    """The answer to one command, with nothing of the socket in it."""
    if not allowed(argv, groups):
        return {"out": "", "code": 1,
                "err": "herdr: this box may run %s, not %s\n"
                       % (", ".join(sorted(groups)) or "nothing",
                          " ".join(argv[:1]) or "a bare herdr")}
    try:
        done = subprocess.run([binary] + argv, capture_output=True,
                              text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"out": "", "err": "herdr: %s\n" % exc, "code": 1}
    return {"out": done.stdout, "err": done.stderr, "code": done.returncode}


def serve(name, binary, groups):
    """Listen for one box, answering with the real herdr."""
    return bridge.serve(KIND, name, lambda argv: _run(argv, binary, groups),
                        {"groups": sorted(groups)})


def mount(provider, profile, name):
    """Give this box a stand-in for herdr, if it was told it may have one.

    Returns docker arguments. A box that declares nothing gets nothing, which
    is the default: reaching the terminal manager is a privilege, not a part
    of being a box.
    """
    import shutil

    from .paths import expand

    want = (profile or {}).get("herdr")
    if not want:
        return []
    groups = [ALL] if want is True else [str(g) for g in want]

    # Ask the system where herdr is, rather than writing a path down here. A
    # literal is wrong twice over: it is one machine's layout, and it goes
    # stale the day the tool moves. PATH is extended with the usual place a
    # user installs into, because the broker may be started by something with
    # a bare PATH.
    where = os.pathsep.join([os.environ.get("PATH", ""), expand("~/.local/bin"),
                             "/opt/homebrew/bin", "/usr/local/bin"])
    binary = shutil.which("herdr", path=where)
    if not binary:
        warn("this box asks for herdr, which is not on the path (%s)" % where)
        return []

    return bridge.mount(KIND, name, "broker.box.herdr", [binary] + groups,
                        {"groups": sorted(groups)}, "herdr", [binary], "herdr")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == "serve":
        return serve(argv[1], argv[2], argv[3:] or [ALL])
    sys.stderr.write("usage: herdr serve <box> <binary> [group...]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
