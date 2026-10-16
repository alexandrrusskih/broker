"""What a box carries in its environment, and what it decides for itself."""

import os

from ..out import warn
from .http_mcp import _box_url
from .mcp import HOST_GATEWAY
from .paths import window_key


# What the terminal is, said in the terminal's own terms. Without these the
# container substitutes a plain "xterm" and a C locale: mouse reporting,
# selection and clipboard escapes (OSC 52) stop matching what the outer terminal
# actually speaks, and text in the pane stops selecting.
TERMINAL_ENV = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
                "LANG", "LC_ALL", "LC_CTYPE")

# Left out of what a box carries: see carried() below.
OWN = {"HOME", "USER", "LOGNAME", "PATH", "SHELL", "PWD", "OLDPWD", "TMPDIR",
       "SHLVL", "_", "XPC_SERVICE_NAME", "XPC_FLAGS", "__CF_USER_TEXT_ENCODING",
       "DISPLAY", "SSH_AUTH_SOCK", "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"}


def carried(profile, env, home, box):
    """Everything the box gets from outside, plus what names this run."""
    args = []
    # What the person set in the shell they typed from, carried in as it is.
    # Anything else would mean the box answers a different question than the
    # same command answers outside it: a variable set on the command line — the
    # actor to act as, the ticket being worked on — simply vanished, and the
    # tool inside used its default while the person watched their setting be
    # ignored. Placed FIRST, so everything the box decides for itself below
    # overrides it; and the few that describe THIS machine rather than the work
    # are left out, because inside they mean something else entirely.
    # Left out because inside they name something that is not there: PATH points
    # at this machine's /opt/homebrew, HOME at a directory the box replaces,
    # SSH_AUTH_SOCK at a socket that does not cross. Carrying them in does not
    # pass a setting along, it breaks the box in its first second. The list is
    # short and technical on purpose.
    # ...and whatever else this box would rather not see, said in its own file:
    #
    #   "unset": ["AWS_PROFILE", "EQUILL_*"]
    #
    # A name, or a name ending in * for a family of them. For keeping a
    # machine-wide habit out of one box — the alternative being to remember not
    # to have it set before typing, which nobody does.
    dropped = tuple(profile.get("unset") or ())

    def wanted(name):
        for pattern in dropped:
            if pattern.endswith("*"):
                if name.startswith(pattern[:-1]):
                    return False
            elif name == pattern:
                return False
        return True

    for variable, value in sorted((env or os.environ).items()):
        if variable in OWN or variable.startswith("BROKER_") or not wanted(variable):
            continue
        if "\n" in value or "\0" in value:
            continue  # docker takes one line per variable
        args += ["-e", "%s=%s" % (variable, value)]

    args += ["-e", "HOME=%s" % home, "-e", "USER=%s" % (os.environ.get("USER") or "user")]
    # Which window this is, for tools inside that keep per-run state of their
    # own. The same key `{window}` resolves to in a box's paths, so a tool and
    # the directory it was given agree on what "this run" means.
    args += ["-e", "BROKER_WINDOW_ID=%s" % window_key()]
    # ...and that this IS a box, and which one. Nothing inside could tell: the
    # paths match the host exactly, which is the point, so a tool that must
    # behave differently in here had nothing to ask. A hook is the case this
    # exists for — it reports the chat it is in to a terminal manager that
    # cannot see into a container, and it has to know to do that.
    args += ["-e", "BROKER_BOX=%s" % box]
    # The same fact under the name the bus hooks already read. They are on the
    # host, in the harness settings this box mounts read-only, so they run in
    # here as they do out there — and their box branch was dead, because
    # nothing set this. A box exists to make what works outside work inside.
    args += ["-e", "AGNTBUS_BOX=1"]
    for variable in TERMINAL_ENV:
        if os.environ.get(variable):
            args += ["-e", "%s=%s" % (variable, os.environ[variable])]
    return args


def credentials(provider, env, remote=None):
    """The token for this run, and the git check no mount can satisfy."""
    args = []
    # git refuses to touch a repository it thinks belongs to someone else, and
    # inside a box it always thinks so: Docker Desktop's file sharing does not
    # present ownership consistently — the mounted directory arrives as root
    # while the files inside it arrive as you. Every path in here was named by
    # the box and is mounted from your own machine, so the check has nothing
    # left to protect. Passed as environment config rather than written into
    # ~/.gitconfig: the host's own git is not ours to reconfigure.
    args += ["-e", "GIT_CONFIG_COUNT=1",
            "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=*"]

    # The credentials for this run, and the marker that tells a harness spawning
    # itself inside the box that it is already brokered.
    carried = ["BROKER_ACTIVE"]
    if getattr(provider, "CREDENTIALS", "file") == "env":
        carried.insert(0, provider.ENV_NAME)
    for key in carried:
        if env.get(key):
            args += ["-e", "%s=%s" % (key, env[key])]

    # Where the harness renews its token, when the broker does it. That is the
    # broker's own url, and a broker on this machine is usually configured as
    # http://127.0.0.1:8787 — which inside a box is the container itself. codex
    # failed every refresh in a box with "error sending request for url
    # (http://127.0.0.1:8787/oauthRefresh…)". A box here reaches the host by its
    # gateway name, as the MCP bridges do; a box on another machine reaches
    # nothing at that address, and only a url both machines can see (a tailnet
    # name) will do. Later -e wins, so this replaces what was carried above.
    moved = False
    for key in getattr(provider, "BOX_REFRESH_URLS", ()):
        url = env.get(key)
        box_url = _box_url(url)
        if not box_url:
            continue
        if remote:
            warn("%s renews its token at %s, which a box on %s cannot reach — "
                 "point the broker url at a name both machines see: broker config --url <url>"
                 % (provider.NAME, url.split("?")[0], remote))
            break
        args += ["-e", "%s=%s" % (key, box_url)]
        moved = True
    if moved:
        args += ["--add-host", "%s:host-gateway" % HOST_GATEWAY]
    return args


def companion(base, env):
    """What a companion harness's account put in its environment.

    The box's own harness has its account installed in the environment the
    box is started from, and carried() takes all of it in. A companion's lives
    in a copy, so without this the box got its directories and none of what
    points into them — codex had a profile and no CODEX_HOME telling it where,
    no database of its own, nowhere to renew its token.
    """
    args = []
    for variable, value in sorted(env.items()):
        if base.get(variable) == value or variable in OWN or variable.startswith("BROKER_"):
            continue
        if "\n" in value or "\0" in value:
            continue
        args += ["-e", "%s=%s" % (variable, value)]
    return args


def own(profile):
    """What the box sets for itself. Last, so it wins over everything above."""
    args = []
    # Docker uses the last -e for a repeated name. Put box settings after the
    # harness credentials and MCP inheritance so the box always wins over the
    # launch environment, including variables an MCP server asks to inherit.
    for key, value in sorted((profile.get("env") or {}).items()):
        args += ["-e", "%s=%s" % (key, value)]
    return args
