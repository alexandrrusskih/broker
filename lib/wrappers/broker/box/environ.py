"""What a box carries in its environment, and what it decides for itself."""

import os

from .paths import window_key


# What the terminal is, said in the terminal's own terms. Without these the
# container substitutes a plain "xterm" and a C locale: mouse reporting,
# selection and clipboard escapes (OSC 52) stop matching what the outer terminal
# actually speaks, and text in the pane stops selecting.
TERMINAL_ENV = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
                "LANG", "LC_ALL", "LC_CTYPE")


def carried(profile, env, home):
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
    OWN = {"HOME", "USER", "LOGNAME", "PATH", "SHELL", "PWD", "OLDPWD", "TMPDIR",
           "SHLVL", "_", "XPC_SERVICE_NAME", "XPC_FLAGS", "__CF_USER_TEXT_ENCODING",
           "DISPLAY", "SSH_AUTH_SOCK", "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"}
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
    for variable in TERMINAL_ENV:
        if os.environ.get(variable):
            args += ["-e", "%s=%s" % (variable, os.environ[variable])]
    return args


def credentials(provider, env):
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
