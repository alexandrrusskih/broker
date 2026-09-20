"""Building the container command, and running it."""

import os
import re
import shutil
import subprocess
import sys
import time

from .. import config, mcpbridge
from ..out import die, warn
# Imported as modules, not as names: a test that replaces one of these replaces
# it where it lives, and a bound name here would keep pointing at the original.
from . import boxes, mcp, ssh
from .mcp import HOST_GATEWAY
from .paths import _empty_file, _mount, _passwd_file, _paths, expand, home_dir

# What the terminal is, said in the terminal's own terms. Without these the
# container substitutes a plain "xterm" and a C locale: mouse reporting,
# selection and clipboard escapes (OSC 52) stop matching what the outer terminal
# actually speaks, and text in the pane stops selecting.
TERMINAL_ENV = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
                "LANG", "LC_ALL", "LC_CTYPE")

# What every box gets regardless of harness, read-only. Without a gitconfig the
# tools inside behave subtly differently from the same tools outside: git reads
# history fine, then refuses to commit for want of a user.email — and the
# harness discovers that halfway through a task.
COMMON_RO = ("~/.gitconfig",)

def _write_stub(box, target, message):
    """A stand-in that explains itself and fails, instead of being missing."""
    directory = os.path.join(config.CONFIG_DIR, "box", "stubs", box.replace("/", "_"))
    path = os.path.join(directory, os.path.basename(target) or "stub")
    body = "#!/bin/sh\n# Written by the broker for the '%s' box.\nprintf '%%s\\n' %s >&2\nexit 127\n" % (
        box, "'" + message.replace("'", "'\\''") + "'")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # In place, not replaced: a box already running has this inode mounted.
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    except OSError as exc:
        warn("could not write the stub for %s (%s)" % (target, exc))
        return None
    return path


def command(provider, name, profile, argv, env):
    """The full container command line for this run."""
    runtime = profile.get("runtime") or "docker"
    binary = shutil.which(runtime)
    if not binary:
        die("%s is not installed — the '%s' box asks for it" % (runtime, name))

    home = home_dir()
    # A box with its own Dockerfile runs its own image, built from the base.
    image = profile.get("image") or (
        "broker-box-%s" % re.sub(r"[^a-zA-Z0-9_.-]", "-", name).lower()
        if profile.get("dockerfile") else "broker-box")

    cmd = [binary, "run", "--rm", "--init"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        cmd.append("-it")
    # The harness writes as you, not as root: files it creates in the project
    # stay yours, and nothing needs chown afterwards.
    # A box that runs containers starts as root — the daemon needs it — and the
    # entry point drops to this uid before the harness starts. Everything else
    # never becomes root at all.
    wants_docker = bool(profile.get("docker"))
    if wants_docker:
        cmd += ["--privileged", "-e", "BROKER_BOX_DOCKER=1",
                "-e", "BROKER_BOX_UID=%d" % os.getuid(),
                "-e", "BROKER_BOX_GID=%d" % os.getgid(),
                # Its own layer store, kept between runs: no two boxes share
                # images, and a test suite does not re-pull Postgres every time.
                #
                # Per WINDOW, not just per box: a docker daemon owns
                # /var/lib/docker exclusively, so a second box of the same name
                # — another pane, another agent — found the store taken and
                # started without a daemon at all. Reopening the same window
                # still reuses its images.
                "--mount", "type=volume,source=broker-box-docker-%s%s,target=/var/lib/docker"
                % (name, ("-" + mcpbridge.identity_key()) if mcpbridge.identity_key() else "")]
    else:
        cmd += ["--user", "%d:%d" % (os.getuid(), os.getgid())]
    cmd += ["-e", "HOME=%s" % home, "-e", "USER=%s" % (os.environ.get("USER") or "user")]
    for variable in TERMINAL_ENV:
        if os.environ.get(variable):
            cmd += ["-e", "%s=%s" % (variable, os.environ[variable])]
    # $HOME itself is a tmpfs owned by that uid. Without it the harness cannot
    # write to its own home: the container creates missing mount points as root,
    # and mounting the real home instead would hand the box everything in it.
    # The directories below land on top of this, so what is mounted survives and
    # what is not is discarded with the container.
    cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700" % (home, os.getuid(), os.getgid())]
    # ...and the same for ~/.config, which tools expect to be able to write to.
    # Mounting anything below it makes the container create the directory
    # itself, owned by root — and then `glab` cannot make its config directory
    # and refuses to run at all. Read-only mounts land on top of this.
    cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700"
            % (os.path.join(home, ".config"), os.getuid(), os.getgid())]

    mounted = []
    passwd = _passwd_file(binary, image)
    if passwd:
        cmd += ["--mount", "type=bind,source=%s,target=/etc/passwd,readonly" % passwd]

    for entry in COMMON_RO:
        host = expand(entry)
        if os.path.exists(host):
            cmd += _mount(host, "ro")

    # A command that exists outside but must not run inside, replaced by a note
    # saying what to do instead. Some tools are deliberately absent — a CLI that
    # identifies itself by hostname would introduce itself as a stranger from in
    # here — but an agent told to run one does not know that: it searches the
    # whole disk for the binary, finds nothing, and asks where it lives.
    #
    #   "stubs": { "~/bin/thing": "not available in a box; use its MCP tools" }
    for target, message in sorted((profile.get("stubs") or {}).items()):
        stub = _write_stub(name, expand(target), str(message))
        if stub:
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly" % (stub, expand(target))]

    # Named keys only, at their own paths, so a tool that resolves ~/.ssh/<name>
    # finds what it expects and nothing else is there to find.
    ssh_config, ssh_keys, ssh_known = ssh._ssh_config(name, profile)
    if ssh_config:
        cmd += ["--mount", "type=bind,source=%s,target=%s,readonly" % (ssh_config, os.path.join(home, ".ssh", "config"))]
        for key in ssh_keys:
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly" % (key, key)]
        if ssh_known:
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly"
                    % (ssh_known, os.path.join(home, ".ssh", "known_hosts"))]

    # The harness's own directory: settings, MCP servers, agents, history.
    #
    # Directories are mounted; single FILES are copied in fresh instead. A bind
    # mount of a file pins one inode, and these files are rewritten atomically —
    # written beside, then renamed over. After the first such write the mount
    # points at an inode nothing links to any more, and inside the box the file
    # has simply vanished: claude reported ~/.claude.json missing and started
    # offering to restore it from a backup, while the host's copy was fine.
    for entry in getattr(provider, "BOX_HOME", ()):
        host = expand(entry)
        if not os.path.exists(host):
            continue
        if os.path.isdir(host):
            cmd += _mount(host)
            mounted.append(host)
            continue
        copy = os.path.join(config.CONFIG_DIR, "box", "files", name.replace("/", "_"),
                            os.path.basename(host))
        try:
            os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
            shutil.copy2(host, copy)
        except OSError as exc:
            warn("could not stage %s for the box (%s) — it will be missing inside" % (host, exc))
            continue
        cmd += ["--mount", "type=bind,source=%s,target=%s" % (copy, host)]
    # ...minus its credentials file. The token comes from the broker below.
    blank = None
    for entry in getattr(provider, "BOX_SECRETS", ()):
        host = expand(entry)
        if any(host.startswith(m + os.sep) for m in mounted):
            blank = blank or _empty_file()
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly" % (blank, host)]

    writable = list(_paths(profile, "rw"))
    for host, target in writable:
        if not os.path.isdir(host):
            die("the '%s' box lists %s, which does not exist" % (name, host))
        cmd += _mount(host, "rw", target)
    for host, target in _paths(profile, "ro"):
        if not os.path.isdir(host):
            die("the '%s' box lists %s, which does not exist" % (name, host))
        cmd += _mount(host, "ro", target)

    # Where work happens: the paths as the box sees them.
    projects = [target for _, target in writable]

    # Start where you started, when that is inside the box; otherwise in the
    # first writable project, so a bare `claude --box work` lands somewhere real.
    # The physical path, not the one you typed. Tools that key work off the
    # directory resolve symlinks first, and a run started from ~/Projects/foo —
    # a link to /Volumes/.../foo — is not recognised as the same project: here
    # that sent a build to the wrong repository identity and failed it on a
    # missing package, which looks nothing like a path problem.
    cwd = os.path.realpath(os.getcwd())
    inside = any(cwd == p or cwd.startswith(p + os.sep) for p in map(os.path.realpath, projects))
    cmd += ["-w", cwd if inside else (os.path.realpath(projects[0]) if projects else home)]

    # The harness's config directory, wherever this run was pointed at: a
    # per-account profile for a file-credentials provider, and for claude
    # whatever CLAUDE_CONFIG_DIR says — a terminal manager gives each agent its
    # own, with the hooks it reports its state through. Not mounting it left the
    # harness running fine and the manager blind to it.
    home_env = getattr(provider, "HOME_ENV", None)
    if home_env and home_env != "HOME" and env.get(home_env):
        host = os.path.abspath(os.path.expanduser(env[home_env]))
        if os.path.isdir(host) and host not in mounted:
            cmd += _mount(host)
            cmd += ["-e", "%s=%s" % (home_env, host)]

    # git refuses to touch a repository it thinks belongs to someone else, and
    # inside a box it always thinks so: Docker Desktop's file sharing does not
    # present ownership consistently — the mounted directory arrives as root
    # while the files inside it arrive as you. Every path in here was named by
    # the box and is mounted from your own machine, so the check has nothing
    # left to protect. Passed as environment config rather than written into
    # ~/.gitconfig: the host's own git is not ours to reconfigure.
    cmd += ["-e", "GIT_CONFIG_COUNT=1",
            "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=*"]

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

    # MCP servers that exist only on this machine. Half of them cannot come
    # along at all — some are native macOS binaries — so the server
    # stays on the host and only its stdio is carried across. The shim is
    # mounted AT THE COMMAND'S OWN PATH, which means the harness's own config
    # needs no rewriting: it already points there. Servers reached over http are
    # left alone; the box can dial them itself.
    inherited = {}
    if profile.get("mcp") is not False:
        claimed = {}
        for name, server in sorted(mcp.mcp_servers(provider, env).items()):
            target = server["command"][0]
            if target in claimed:
                warn("%s and %s start from the same command (%s) — only the first is bridged"
                     % (claimed[target], name, target))
                continue
            live = mcp._start_bridge(name, server, profile, projects)
            if not live:
                continue
            for variable in server.get("inherit") or []:
                if os.environ.get(variable) and variable not in inherited:
                    inherited[variable] = os.environ[variable]
            claimed[target] = name
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly"
                    % (mcp._write_shim(provider, name, live), target)]
        for variable, value in sorted(inherited.items()):
            cmd += ["-e", "%s=%s" % (variable, value)]
        if claimed:
            # Docker Desktop resolves this name already; Colima and plain Linux
            # need to be told, and saying it twice costs nothing.
            cmd += ["--add-host", "%s:host-gateway" % HOST_GATEWAY]

    cmd.append(image)
    cmd.append("broker-box-entry")
    cmd.append(provider.BIN)

    # Flags the box hands the harness, before what you typed — so a flag you
    # pass on the command line still wins. This is where a box says how much it
    # trusts what runs inside it: the broker does not decide that for you, and
    # nothing here is implied by a box existing. Per harness, because they spell
    # the same idea differently:
    #
    #   "args": { "claude": ["--dangerously-skip-permissions"] }
    #
    # A plain list applies to every harness in the box.
    extra = profile.get("args")
    if isinstance(extra, dict):
        extra = extra.get(provider.NAME) or []
    for flag in extra or []:
        if flag not in argv:
            cmd.append(str(flag))

    cmd += argv
    return cmd


def _last_session(provider, workdir, env=None, since=0):
    """The session just written, as this harness records them.

    `since` is when the box started. Only claude files its sessions under the
    working directory; codex and agy keep one pile each, so without it the
    newest file could belong to a run in another window entirely.
    """
    pattern = getattr(provider, "SESSION_GLOB", None)
    if not pattern:
        return None
    import glob as globmodule

    home_env = getattr(provider, "HOME_ENV", None)
    fields = {
        "key": workdir.replace(os.sep, "-"),
        "home": home_dir(),
        # Where this run's own settings live: a per-account profile, or the
        # harness's usual directory when nothing was pointed elsewhere.
        "config": (env or {}).get(home_env) if home_env and home_env != "HOME" else None,
    }
    if fields["config"] is None:
        fields["config"] = expand(getattr(provider, "CANONICAL_HOME", "~"))

    found = [f for f in globmodule.glob(pattern % fields) if os.path.getmtime(f) >= since]
    if not found:
        return None
    newest = max(found, key=lambda f: os.path.getmtime(f))
    stem = os.path.splitext(os.path.basename(newest))[0]
    # Some name the file after the session; others prefix it with a timestamp
    # and leave the id at the end.
    match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", stem)
    return match.group(0) if match else stem


def _resume_hint(provider, name, workdir, env=None, since=0, account=None):
    """What to type to come back INTO this box, on the same account.

    The harness prints its own resume line as it exits, and that line is missing
    the box: run it as printed and the session reopens on the host, in a
    different world, which is not obvious until something behaves oddly.

    And for a harness whose sessions live inside a per-account profile, the
    account matters as much as the box. The broker moves to another account when
    one runs out of room, and the session recorded under the first is then not
    found at all: "no rollout found for thread id". Naming the account makes the
    line reopen what it says it will.
    """
    session = _last_session(provider, workdir, env, since)
    if not session:
        return None
    # Each harness spells resuming its own way.
    resume = getattr(provider, "SESSION_RESUME", "--resume %s") % session
    pin = ""
    if account and getattr(provider, "CREDENTIALS", "file") != "env":
        pin = "%s_ACCOUNT=%s " % (provider.NAME.upper(), account)
    return "\nResume it in this box with:\n  %s%s --box %s %s\n" % (pin, provider.BIN, name, resume)


def exec_box(provider, name, argv, env, account=None):
    """Run the container, then say how to come back to it."""
    defined = boxes.profiles()
    if name not in defined:
        die("no box called '%s' in %s%s" % (
            name, boxes.PATH,
            (" — defined: " + ", ".join(sorted(defined))) if defined else " (the file does not exist yet)"))
    # A terminal manager watches the pane's foreground process to know what runs
    # in it. From here on that process is `docker`, which hides the harness
    # behind it — Herdr documents the way out: a wrapper says which agent it
    # stands for, and the manager reads its screen as it would any other.
    if os.environ.get("HERDR_PANE_ID") and not os.environ.get("HERDR_AGENT"):
        os.environ["HERDR_AGENT"] = provider.NAME

    cmd = command(provider, name, defined[name], argv, env)
    workdir = cmd[cmd.index("-w") + 1] if "-w" in cmd else os.getcwd()
    started = time.time()

    # Waited for rather than exec'd into, only so the box can add its own line
    # after the harness has printed its resume hint. Everything else about the
    # run is unchanged: stdio is inherited, so the terminal, the mouse and the
    # clipboard behave as if nothing sat in between, and `docker run -it`
    # forwards the signals itself.
    try:
        finished = subprocess.run(cmd)
    except KeyboardInterrupt:
        sys.exit(130)
    except OSError as exc:
        die("cannot start the '%s' box: %s" % (name, exc))

    hint = _resume_hint(provider, name, workdir, env, started, account)
    if hint and finished.returncode == 0:
        sys.stdout.write(hint)
    sys.exit(finished.returncode)
