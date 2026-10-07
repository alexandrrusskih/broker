"""Building the container command, and running it."""

import json
import os
import re
import shlex
import pty
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import uuid

from .. import config, mcpbridge
from ..out import die, warn
# Imported as modules, not as names: a test that replaces one of these replaces
# it where it lives, and a bound name here would keep pointing at the original.
from . import boxes, http_mcp, mcp, ssh
from .mcp import HOST_GATEWAY
from .sync import SYNC_LOG, _StoreLock, _sync_back
from .sessions import (LOG, SAID_ID, SESSION_ID, _created, _exit_note, _id_shape, _pin_session,
                      _private_store, _remember, _resume_hint, _session_from_argv,
                      _session_from_store, _session_id, _session_it_named, _session_path,
                      _session_root, _sessions_are_shared)
from .terminal import (TERMINAL_RESET, _guard_terminal, _restore_terminal,
                      _terminal_state, _through_terminal)
from .paths import (_bind, _empty_file, _mount, _passwd_file, _paths, expand, home_dir,
                    window_key)

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

def _clone(source, destination):
    """Copy that costs nothing until something is written. Files or trees.

    These files are databases, and one of them is nearly four gigabytes; copying
    that on every start would be absurd. APFS clones instead: the copy shares
    the same blocks until one side changes them, which is exactly the shape of
    this — a box reads almost all of it and writes a little. `cp -c` asks for
    that and falls back on its own when the filesystem cannot.
    """
    if os.path.isdir(destination):
        shutil.rmtree(destination)
    elif os.path.exists(destination):
        os.remove(destination)
    try:
        subprocess.run(["cp", "-c", "-R", source, destination], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        if os.path.isdir(source):
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)


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


# A pull-through cache for the images a box's own docker daemon fetches, run
# once on this machine and shared by every box on it.
#
# Each window's daemon owns its own layer store — two daemons cannot share one
# /var/lib/docker — so every new window used to fetch Postgres and Redis from
# the internet again, and a person who opens and kills boxes all day pays that
# download every time. The cache turns the second fetch and every one after it
# into a copy over the loopback.
#
# Bound to 127.0.0.1: it holds public images and answers only this machine.
REGISTRY_CACHE = "broker-box-registry"
REGISTRY_PORT = 5009
REGISTRY_MIRROR = "http://%s:%d" % (mcp.HOST_GATEWAY, REGISTRY_PORT)


def _ensure_registry_cache(binary):
    """Have the cache running, or carry on without it.

    Never fatal: a box whose images come straight from the internet works
    exactly as it did before, only slower on a cold window.
    """
    try:
        state = subprocess.run([binary, "inspect", "-f", "{{.State.Running}}", REGISTRY_CACHE],
                               capture_output=True, text=True, timeout=20)
        if state.returncode == 0:
            if state.stdout.strip() == "true":
                return True
            return subprocess.run([binary, "start", REGISTRY_CACHE],
                                  capture_output=True, timeout=30).returncode == 0
        started = subprocess.run(
            [binary, "run", "-d", "--name", REGISTRY_CACHE, "--restart", "unless-stopped",
             "-p", "127.0.0.1:%d:5000" % REGISTRY_PORT,
             "-v", "%s-cache:/var/lib/registry" % REGISTRY_CACHE,
             "-e", "REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io",
             "registry:2"], capture_output=True, text=True, timeout=180)
        if started.returncode:
            # One line of prose, not a list: `% a[-1:] or ""` binds as
            # `("…%s" % a[-1:]) or ""`, so this printed the repr of a one-item
            # list — brackets, quotes and all — around the daemon's own words.
            last = (started.stderr or "").strip().splitlines()
            warn("the image cache did not start, images will come from the internet: %s"
                 % (last[-1].strip() if last else "no reason given"))
        return started.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _daemon_is_up(binary):
    """Whether this runtime's daemon answers at all.

    `version` and not `info`: it is the one call that talks to the server and
    returns before anything is enumerated — 56ms here against a live daemon,
    and a non-zero exit the moment the socket is dead.
    """
    try:
        probe = subprocess.run([binary, "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and bool(probe.stdout.strip())


def command(provider, name, profile, argv, env, remote=False):
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
                # Sanitised like the image name: docker accepts only
                # [a-zA-Z0-9][a-zA-Z0-9_.-] in a volume name, and a box named in
                # anything else fails to start at all.
                "--mount", "type=volume,source=broker-box-docker-%s%s,target=/var/lib/docker"
                % (re.sub(r"[^a-zA-Z0-9_.-]", "-", name).lower(),
                   ("-" + mcpbridge.identity_key()) if mcpbridge.identity_key() else "")]
        # Where that daemon looks before the internet. The name resolves through
        # the host-gateway line added below.
        if _ensure_registry_cache(binary):
            cmd += ["-e", "BROKER_BOX_REGISTRY_MIRROR=%s" % REGISTRY_MIRROR]
        cmd += ["--add-host", "%s:host-gateway" % mcp.HOST_GATEWAY]
    else:
        cmd += ["--user", "%d:%d" % (os.getuid(), os.getgid())]
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
        cmd += ["-e", "%s=%s" % (variable, value)]

    cmd += ["-e", "HOME=%s" % home, "-e", "USER=%s" % (os.environ.get("USER") or "user")]
    # Which window this is, for tools inside that keep per-run state of their
    # own. The same key `{window}` resolves to in a box's paths, so a tool and
    # the directory it was given agree on what "this run" means.
    cmd += ["-e", "BROKER_WINDOW_ID=%s" % window_key()]
    for variable in TERMINAL_ENV:
        if os.environ.get(variable):
            cmd += ["-e", "%s=%s" % (variable, os.environ[variable])]
    # $HOME itself is a tmpfs owned by that uid. Without it the harness cannot
    # write to its own home: the container creates missing mount points as root,
    # and mounting the real home instead would hand the box everything in it.
    # The directories below land on top of this, so what is mounted survives and
    # what is not is discarded with the container.
    #
    # Executable, deliberately. Docker mounts a tmpfs noexec by default, and
    # that default cost a day here: a browser unpacked into ~/.cache refused to
    # start with EACCES while its permissions read as executable, and it looked
    # like something was wiping the environment. Nothing was. A box is already a
    # container with only the directories it was given — forbidding execution
    # inside its own home protects nothing that the box itself does not.
    cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700,exec" % (home, os.getuid(), os.getgid())]
    # ...and the same for ~/.config, which tools expect to be able to write to.
    # Mounting anything below it makes the container create the directory
    # itself, owned by root — and then `glab` cannot make its config directory
    # and refuses to run at all. Read-only mounts land on top of this.
    cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700,exec"
            % (os.path.join(home, ".config"), os.getuid(), os.getgid())]
    # Bun's shared cache is mounted at ~/.bun/install/cache. Without these
    # parent tmpfs mounts Docker creates ~/.bun and ~/.bun/install as root,
    # leaving `bun link` unable to create install/global as the box user.
    for directory in ("~/.bun", "~/.bun/install"):
        cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700,exec"
                % (expand(directory), os.getuid(), os.getgid())]

    # ...and anywhere else this harness insists on writing. Mounting a file
    # deep under $HOME makes the container create its parents as root, and the
    # harness — which runs as you — then cannot make a sibling directory next
    # to its own database: "EACCES: permission denied, mkdir". Naming the
    # directory here gets it owned by you before anything is mounted into it.
    for directory in getattr(provider, "BOX_WRITABLE", ()):
        cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700,exec"
                % (expand(directory), os.getuid(), os.getgid())]

    mounted = []
    passwd = _passwd_file(binary, image)
    if passwd:
        cmd += _bind(passwd, "/etc/passwd", "ro")

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
            cmd += _bind(stub, expand(target), "ro")

    # Named keys only, at their own paths, so a tool that resolves ~/.ssh/<name>
    # finds what it expects and nothing else is there to find.
    ssh_config, ssh_keys, ssh_known = ssh._ssh_config(name, profile)
    if ssh_config:
        cmd += _bind(ssh_config, os.path.join(home, ".ssh", "config"), "ro")
        for key in ssh_keys:
            cmd += _bind(key, key, "ro")
        if ssh_known:
            cmd += _bind(ssh_known, os.path.join(home, ".ssh", "known_hosts"), "ro")

    # The harness's own directory: settings, MCP servers, agents, history.
    #
    # Directories are mounted; single FILES are copied in fresh instead. A bind
    # mount of a file pins one inode, and these files are rewritten atomically —
    # written beside, then renamed over. After the first such write the mount
    # points at an inode nothing links to any more, and inside the box the file
    # has simply vanished: claude reported ~/.claude.json missing and started
    # offering to restore it from a backup, while the host's copy was fine.
    http_config = None if profile.get("mcp") is False or provider.NAME == "codex" else http_mcp.stage(provider, env, name)
    http_config_mounted = False
    http_mode = "rw" if provider.NAME == "claude" else "ro"
    settings_mounted = set()
    for entry in getattr(provider, "BOX_HOME", ()):
        host = expand(entry)
        if not os.path.exists(host):
            continue
        if http_config and host == http_config[1]:
            cmd += _bind(http_config[0], host, http_mode)
            http_config_mounted = True
            settings_mounted.add(host)
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
        mode = "ro" if entry in getattr(provider, "BOX_SETTINGS", ()) else "rw"
        cmd += _bind(copy, host, mode)
        settings_mounted.add(host)
    # ...minus its credentials file. The token comes from the broker below.
    blank = None
    for entry in getattr(provider, "BOX_SECRETS", ()):
        host = expand(entry)
        if any(host.startswith(m + os.sep) for m in mounted):
            blank = blank or _empty_file()
            cmd += _bind(blank, host, "ro")

    # This window's own databases, at the same path inside as outside. The
    # harness is pointed at them by its own variable (see the provider), so a
    # box needs nothing else — no clone of each file, no mount per file. That
    # matters beyond tidiness: a database mounted AS A FILE cannot be renamed,
    # and this harness recovers from a damaged one by moving it aside. In a box
    # that move failed with "Resource busy" and the start was refused outright,
    # over a file it was perfectly willing to rebuild.
    sqlite_env = getattr(provider, "SQLITE_ENV", None)
    own_databases = (env or {}).get(sqlite_env) if sqlite_env else None
    if own_databases:
        try:
            os.makedirs(own_databases, mode=0o700, exist_ok=True)
        except OSError as exc:
            warn("could not make %s for the box (%s)" % (own_databases, exc))
        else:
            cmd += _mount(own_databases, "rw")

    # GitLab, which is authenticated by a variable everywhere except in its own
    # status command.
    #
    # The tool reads GITLAB_TOKEN happily — an API call from inside a box works
    # — but `glab auth status` answers "has not been authenticated", because it
    # looks for a host it knows, and on this machine that knowledge lives in
    # the Keychain, which does not cross into a container. So anything that
    # checks before it acts stops there and says to log in, over a login that
    # is already good.
    #
    # The file says what the Keychain would have said. Written per box, 0600,
    # from the token already in this environment — nothing new is stored that
    # was not being passed in anyway.
    # ...and only for a box that was given the key to that host. A token in the
    # environment reaches every box, because the environment does; the boxes
    # that are meant to push there are the ones this file names. Saying it any
    # other way would hand a credential to boxes that were never asked to have
    # one.
    token = (env or {}).get("GITLAB_TOKEN")
    host = (env or {}).get("GITLAB_HOST")
    if token and host and host in ((profile.get("ssh") or {}).get("hosts") or {}):
        where = os.path.join(config.CONFIG_DIR, "box", "glab",
                             re.sub(r"[^A-Za-z0-9_.-]", "-", name), "config.yml")
        try:
            os.makedirs(os.path.dirname(where), mode=0o700, exist_ok=True)
            with open(where, "w") as handle:
                handle.write("hosts:\n  %s:\n    token: %s\n"
                             "    api_protocol: https\n    git_protocol: ssh\n"
                             % (host, token))
            os.chmod(where, 0o600)
        except OSError as exc:
            warn("could not write the glab config for the box (%s)" % exc)
        else:
            cmd += _bind(where, os.path.join(home, ".config", "glab-cli", "config.yml"), "ro")

    # Files a box gets EMPTY and keeps to itself: it may write them, and what
    # it writes stays inside. Not a secret it must not see (that is above, and
    # is read-only) — a file this harness rewrites WHOLE, dropping whatever it
    # did not put there itself.
    #
    # agy's MCP tokens are the case this exists for. Mounted straight through,
    # a box emptied that file and two logins granted an hour earlier were gone,
    # reported afterwards as "Unauthorized [Auth Needed]" — which reads as an
    # expired token rather than as a file cleared by a program next door. Four
    # times in one afternoon.
    #
    # Read-only would stop the damage and cost more than it saves: the harness
    # then fails on a write it expects to succeed, rather than carrying on
    # without the servers that need a login. So: its own copy, writable,
    # starting out empty, never seeded from here. A box that wants those
    # servers logs in for itself; a box that does not is no worse off than one
    # that never had the file.
    for entry, seed in getattr(provider, "BOX_BLANK", ()):
        host = expand(entry)
        if not any(host.startswith(m + os.sep) for m in mounted):
            continue
        own = os.path.join(config.CONFIG_DIR, "box", "blank", name.replace("/", "_"),
                           host.lstrip(os.sep).replace(os.sep, "_"))
        try:
            os.makedirs(os.path.dirname(own), mode=0o700, exist_ok=True)
            if not os.path.exists(own):
                with open(own, "w") as handle:
                    handle.write(seed)
                os.chmod(own, 0o600)
        except OSError as exc:
            warn("could not give the box a blank %s (%s)" % (entry, exc))
            continue
        cmd += _bind(own, host)

    # Files a box may READ but must not touch. Not the same as a secret it may
    # not see at all: a harness inside needs these to work, and needs them to
    # be the real ones. agy's MCP tokens are the case this exists for — started
    # in a box it rewrote that file EMPTY, and two logins granted an hour
    # earlier were gone, reported afterwards as "Unauthorized [Auth Needed]",
    # which reads as an expired token rather than as a file destroyed by a
    # program on the same machine. Mounted read-only, the worst it can do is
    # fail to write.
    for entry in getattr(provider, "BOX_READONLY", ()):
        host = expand(entry)
        if os.path.exists(host) and any(host.startswith(m + os.sep) for m in mounted):
            cmd += _bind(host, host, "ro")

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

    # What the BOX says it wants its own copy of — same reasoning as the
    # provider's own list, but for anything else on the machine: credentials
    # with a token database inside, caches that a tool rewrites in place.
    #
    #   "private": ["~/.config/gcloud"]
    #
    # Cloned, so it costs nothing until written, and what the box writes stays
    # in the box.
    for entry in (profile.get("private") or []):
        host = expand(entry)
        if not os.path.exists(host):
            continue
        copy = os.path.join(config.CONFIG_DIR, "box", "private",
                            name.replace("/", "_"), "own",
                            os.path.basename(host.rstrip(os.sep)))
        try:
            os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
            # Every start, not only when it looks newer. A directory's mtime
            # does not change when a file INSIDE it is rewritten — and that is
            # exactly what a login does to a token database. Comparing
            # timestamps kept handing the box yesterday's credentials.
            #
            # Except for what the box is expected to WRITE and keep: a login
            # done inside belongs to that box, and copying the machine's copy
            # over it on the next start throws the login away. Seeded once,
            # then left alone — which is how a container that authenticates
            # once and keeps working is usually set up.
            if entry in getattr(provider, "BOX_KEEPS", ()) and os.path.exists(copy):
                pass
            else:
                _clone(os.path.realpath(host), copy)
        except OSError as exc:
            warn("could not give the box its own %s (%s)" % (entry, exc))
            continue
        cmd += _bind(copy, host)

    # Files a box must not share with the host, however they got there: cloned
    # in, so writing them inside changes nothing outside. One clone per real
    # file — every profile's copy is a symlink to the same one — mounted at each
    # name the harness might open it by.
    private = getattr(provider, "BOX_PRIVATE", ())
    home_env_now = getattr(provider, "HOME_ENV", None)
    in_use = (env or {}).get(home_env_now) if home_env_now and home_env_now != "HOME" else None
    if private:
        import glob as globmodule

        clones = {}
        # Nobody may be registering a session out here while these are copied.
        store_lock = _StoreLock(provider)
        store_lock.__enter__()
        for base in [expand(getattr(provider, "CANONICAL_HOME", "~"))] + ([expand(in_use)] if in_use else []):
            for pattern in private:
                for host in sorted(globmodule.glob(os.path.join(base, pattern))):
                    real = os.path.realpath(host)
                    copy = clones.get(real)
                    if copy is None:
                        copy = os.path.join(config.CONFIG_DIR, "box", "private",
                                            name.replace("/", "_"), provider.NAME,
                                            os.path.basename(real))
                        try:
                            os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
                            # Fresh every start: cloning costs nothing, and a
                            # stale copy is worse than none.
                            _clone(real, copy)
                        except OSError as exc:
                            warn("could not give the box its own %s (%s)" % (os.path.basename(real), exc))
                            continue
                        clones[real] = copy
                    cmd += _bind(copy, host)
                    # sqlite keeps its write-ahead log and shared-memory file
                    # BESIDE the database, and those are part of its state: the
                    # newest pages live in -wal until a checkpoint folds them
                    # in. Cloning the database alone leaves them coming from
                    # the directory mount underneath — so the box wrote its
                    # pages into its own copy and its journal into everyone's,
                    # and both tore. Measured as "wrong # of entries in index"
                    # on the host while a box was running.
                    #
                    # Mounted even when the host has no such file yet, or
                    # sqlite would create one in the shared directory the
                    # moment it opens the database.
                    for side in ("-wal", "-shm"):
                        beside = copy + side
                        try:
                            if os.path.exists(real + side):
                                _clone(real + side, beside)
                            elif not os.path.exists(beside):
                                open(beside, "a").close()
                        except OSError as exc:
                            warn("could not give the box its own %s (%s)"
                                 % (os.path.basename(real) + side, exc))
                            continue
                        cmd += _bind(beside, host + side)

        # Copied; whoever wants to register a session may go ahead.
        store_lock.__exit__()

    # Directories that every profile of this harness should reach, not only the
    # one this run uses: a harness can record a path through a profile it is no
    # longer using, and the file it names is shared anyway.
    shared = getattr(provider, "BOX_SHARED", ())
    canonical = expand(getattr(provider, "CANONICAL_HOME", "~"))
    if shared:
        import glob as globmodule

        for sibling in sorted(globmodule.glob(canonical + "-*")):
            for entry in shared:
                host = os.path.join(sibling, entry)
                if os.path.exists(host):
                    cmd += _bind(os.path.realpath(host), host)

    # The harness's config directory, wherever this run was pointed at: a
    # per-account profile for a file-credentials provider, and for claude
    # whatever CLAUDE_CONFIG_DIR says — a terminal manager gives each agent its
    # own, with the hooks it reports its state through. Not mounting it left the
    # harness running fine and the manager blind to it.
    home_env = getattr(provider, "HOME_ENV", None)
    if home_env and home_env != "HOME" and env.get(home_env):
        host = os.path.abspath(os.path.expanduser(env[home_env]))
        if os.path.isdir(host) and host not in mounted:
            source = host
            if provider.NAME == "codex" and host != canonical and not remote:
                root = os.path.join(config.CONFIG_DIR, "box", "profiles",
                                    name.replace("/", "_"))
                os.makedirs(root, mode=0o700, exist_ok=True)
                source = os.path.join(root, uuid.uuid4().hex)
                shutil.copytree(host, source, symlinks=True)
            cmd += _mount(source, target=host)
            cmd += ["-e", "%s=%s" % (home_env, host)]
    if http_config and not http_config_mounted:
        cmd += _bind(http_config[0], http_config[2], http_mode)

    # Host settings stay read-only. Runtime state in the same home stays writable.
    config_target = http_config[1] if http_config else None
    for entry in getattr(provider, "BOX_SETTINGS", ()):
        host = expand(entry)
        if os.path.isfile(host) and host != config_target and host not in settings_mounted:
            cmd += _mount(os.path.realpath(host), "ro", host)
    if provider.NAME == "codex" and env.get("CODEX_HOME"):
        profile_home = os.path.abspath(expand(env["CODEX_HOME"]))
        if profile_home != canonical:
            for filename in ("config.toml", "hooks.json"):
                host = os.path.join(profile_home, filename)
                if os.path.isfile(host):
                    cmd += _mount(os.path.realpath(host), "ro", host)

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
            cmd += _bind(mcp._write_shim(provider, name, live), target, "ro")
        for variable, value in sorted(inherited.items()):
            cmd += ["-e", "%s=%s" % (variable, value)]
        if claimed:
            # Docker Desktop resolves this name already; Colima and plain Linux
            # need to be told, and saying it twice costs nothing.
            cmd += ["--add-host", "%s:host-gateway" % HOST_GATEWAY]

    # Docker uses the last -e for a repeated name. Put box settings after the
    # harness credentials and MCP inheritance so the box always wins over the
    # launch environment, including variables an MCP server asks to inherit.
    for key, value in sorted((profile.get("env") or {}).items()):
        cmd += ["-e", "%s=%s" % (key, value)]

    cmd.append(image)
    cmd.append("broker-box-entry")
    cmd.append(provider.BIN)
    if profile.get("mcp") is not False:
        cmd += http_mcp.codex_args(provider, env)

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




















































def _over_ssh(cmd, machine, name):
    """The same container command, run on another machine instead of this one.

    Only the terminal stays here. The image, the project and the harness are all
    over there, so the paths in the command are that machine's paths — a box
    describes what it may touch, and a machine says where those things live on
    it. Anything the machine does not redirect is passed through unchanged,
    which is right when both sides keep a project in the same place and wrong
    silently when they do not, so a machine that differs must say so.
    """
    swaps = sorted((machine.get("paths") or {}).items(), key=lambda kv: -len(kv[0]))

    def moved(value):
        for mine, theirs in swaps:
            mine = expand(mine)
            if value == mine or value.startswith(mine + os.sep):
                return expand(theirs) + value[len(mine):]
        return value

    # ONLY the source half of a bind mount is a path on the other machine.
    # Everything else that looks like a path — the target of a mount, the
    # working directory, a tmpfs, $HOME — is a path INSIDE the container, and
    # the whole point of a box is that those stay the same wherever it runs.
    # Rewriting them would move the box's own furniture and break --resume.
    out = []
    for part in cmd:
        if part.startswith("type=bind,source="):
            head, _, rest = part.partition("source=")
            source, sep, tail = rest.partition(",")
            out.append(head + "source=" + moved(source) + sep + tail)
        else:
            out.append(part)

    target = machine.get("ssh")
    if not target:
        die("machine '%s' has no \"ssh\" target in %s" % (name, boxes.PATH))
    # -t: a harness is a full-screen program and needs a terminal on the far
    # side. Without it the pane comes up in line mode and nothing redraws.
    ssh = ["ssh", "-t", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=120", target]
    return ssh + ["--"] + [shlex.quote(part) for part in out]


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

    remote, machine, argv = boxes.take_remote(argv)
    # Asked for before anything is built on the assumption it is there. Without
    # this, a stopped Docker produced three messages and no answer: the registry
    # cache warned (it is never allowed to be fatal), `docker run` printed the
    # daemon's own "Cannot connect" line, and then this function said "Resume it
    # in this box with: …" — the command that had just failed, offered as the way
    # back into a box that never came up.
    #
    # Only for a local run: a remote box's daemon is on the far machine, and the
    # one here may well be stopped and irrelevant.
    if not remote:
        runtime = defined[name].get("runtime") or "docker"
        binary = shutil.which(runtime)
        if binary and not _daemon_is_up(binary):
            die("the %s daemon is not running — start it, then try again" % runtime)
    pinned, argv = _pin_session(provider, argv)
    cmd = command(provider, name, defined[name], argv, env, remote=bool(remote))
    shadow_root = os.path.join(config.CONFIG_DIR, "box", "profiles") + os.sep
    shadows = [part.partition("source=")[2].partition(",")[0]
               for part in cmd if part.startswith("type=bind,source=")
               and part.partition("source=")[2].startswith(shadow_root)]
    if remote:
        # Sessions, databases and MCP bridges all belong to the machine the box
        # runs on, and none of them reach across. Say so once, rather than let
        # it be discovered by something behaving oddly.
        warn("box '%s' runs on %s: its sessions and bridged MCP servers live there, not here"
             % (name, remote))
        cmd = _over_ssh(cmd, machine, remote)
    workdir = cmd[cmd.index("-w") + 1] if "-w" in cmd else os.getcwd()
    started = time.time()

    # Waited for rather than exec'd into, only so the box can add its own line
    # after the harness has printed its resume hint. Everything else about the
    # run is unchanged: stdio is inherited, so the terminal, the mouse and the
    # clipboard behave as if nothing sat in between, and `docker run -it`
    # forwards the signals itself.
    saved = _terminal_state()
    _guard_terminal(saved)
    status = 0
    printed = b""
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            # Watched, so the id the harness names on its way out can be read
            # rather than guessed at afterwards.
            status, printed = _through_terminal(cmd)
        else:
            status = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        # Ctrl-C reaches this process as well as the container. The harness
        # inside still exits properly and its session file is complete, so the
        # rest of this — folding the session back, saying how to return — is
        # exactly as valuable as after an ordinary exit.
        status = 130
    except OSError as exc:
        die("cannot start the '%s' box: %s" % (name, exc))
    finally:
        # Whatever happened in there — a clean exit, a crash, `docker stop` from
        # another window — the pane is usable again from this line on.
        _restore_terminal(saved)
        for shadow in shadows:
            shutil.rmtree(shadow, ignore_errors=True)

    # 125 is the one exit code docker keeps for itself: the CLI could not run the
    # container at all. Nothing ran in there, so there is no session to fold back
    # and nothing to resume — and everything below this line is about a box that
    # held a conversation. Said as a failure instead, which is what it is.
    if status == 125 and not printed.strip():
        _remember(provider, name, workdir, None, account, status)
        die("the '%s' box did not start — the reason is above" % name, status)

    # What the harness itself recorded, where it could not be confused with
    # another window's — better than any guess made from file times.
    # What the harness said, in its own words, beats anything worked out from
    # file times in a directory every window writes into.
    # Only what the harness itself named. There used to be a fallback that
    # worked the id out from file times, and it was worse than nothing: every
    # window on this machine writes its sessions into one directory, so the
    # "newest file" belongs to whoever typed last. Two boxes started together
    # were handed the same id, and it belonged to neither. A printed id has to
    # mean something; when there is none, say so.
    # Two sources, both of them plain fact: what the harness printed about
    # itself, and what was typed to start it. A third was tried — digging the
    # thread id out of the harness's own log — and it came back with the middle
    # of a sentence. Anything that has to be checked for being nonsense before
    # it can be printed is not a source, it is a guess with paperwork.
    session = (pinned
               or _session_it_named(printed, getattr(provider, "SESSION_PRINTED", None), provider)
               or _session_from_argv(provider, argv))
    _remember(provider, name, workdir, session, account, status)
    if session is None:
        session = _session_from_store(provider, name, started)
    # Said whatever happened, including when there is no id to say: a box that
    # goes quiet leaves someone staring at a prompt wondering what became of an
    # hour's conversation. Without an id the line points at the harness's own
    # picker, which is a poorer answer than the id and a far better one than
    # silence. This used to sit behind "if we know the session", so the very
    # case that needed saying out loud was the one that said nothing.
    #
    # Printed BEFORE the session is folded back: folding asks the harness to
    # re-read its own session, and it looks through every session it has to
    # find the one named — thirteen thousand of them here, which is seconds of
    # silence. The line is what the person is waiting for; the bookkeeping can
    # happen behind it.
    #
    # Registered as well as printed: whatever finishes this process — a signal
    # that got through, an error on the way out — the line still goes. It is
    # the only place the id exists once the screen has been restored.
    import atexit

    printed_once = []

    def say(text):
        if text and text not in printed_once:
            printed_once.append(text)
            try:
                # stderr, like every other message to the operator — out.py says
                # why in its first line: stdout belongs to the harness. This one
                # line broke that rule, and it is the line that shows it. The pty
                # loop above writes the harness's bytes straight to fd 1 with
                # os.write, unbuffered, while this wrote through the buffered
                # sys.stdout layer on the SAME descriptor; the two arrived spliced
                # mid-word, the harness's own resume line tangled into ours.
                # fd 1 is flushed first so what the harness said still lands
                # before what we say about it.
                sys.stdout.flush()
                sys.stderr.write(text)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass

    note = _exit_note(provider, session, workdir, env)
    if note:
        warn("the harness stopped with: %s" % " ".join(note.split()))
    hint = _resume_hint(provider, name, workdir, env, started, account, session)
    atexit.register(say, hint)
    say(hint)
    if session:
        _sync_back(provider, session, env, name)
    sys.exit(status)
