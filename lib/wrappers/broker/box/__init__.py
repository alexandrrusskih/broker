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

Split across four files because each answers a different question: profiles (what
a box is), paths (where things go), mcp and ssh (what a box may reach), run (how
it starts).
"""

from . import boxes, mcp, paths, run, ssh
from .mcp import mcp_servers
from .paths import REAL_HOME_ENV, expand, home_dir
from .boxes import EXAMPLE, FLAG, PATH, profiles, take_flag
from .run import command, exec_box

__all__ = [
    "EXAMPLE", "FLAG", "PATH", "REAL_HOME_ENV",
    "boxes", "mcp", "paths", "run", "ssh",
    "command", "exec_box", "expand", "home_dir", "mcp_servers", "profiles", "take_flag",
]
