"""Building the container command, and running it."""

import os
import re
import shlex
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
from . import boxes, mcp, ssh
from .mcp import HOST_GATEWAY
from .paths import _empty_file, _mount, _passwd_file, _paths, expand, home_dir, window_key

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
            warn("the image cache did not start, images will come from the internet: %s"
                 % started.stderr.strip().splitlines()[-1:] or "")
        return started.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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
            _clone(os.path.realpath(host), copy)
        except OSError as exc:
            warn("could not give the box its own %s (%s)" % (entry, exc))
            continue
        cmd += ["--mount", "type=bind,source=%s,target=%s" % (copy, host)]

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
                    cmd += ["--mount", "type=bind,source=%s,target=%s" % (copy, host)]
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
                        cmd += ["--mount", "type=bind,source=%s,target=%s"
                                % (beside, host + side)]

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
                    cmd += ["--mount", "type=bind,source=%s,target=%s" % (os.path.realpath(host), host)]

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


SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _pin_session(provider, argv):
    """Decide this run's session id before it starts, if the harness lets us.

    Otherwise the box has to work it out afterwards by reading the newest
    session file in the project's directory — which is right only while one
    harness writes there. Every box open on the same project shares that
    directory, so on a busy machine the newest file is somebody else's window,
    and the resume line the box prints leads into a stranger's conversation.
    The id is unguessable but not unknowable: harnesses accept it as an
    argument, so it is chosen here and simply known.

    Returns the id and the arguments to run with. When the person is already
    naming a session — resuming, continuing, picking one from a list — the id
    is theirs: it is read from what they typed, or left unknown when only a
    picker can answer, and nothing is added.
    """
    flag = getattr(provider, "SESSION_ID_FLAG", None)
    if not flag:
        return None, argv
    pickers = getattr(provider, "SESSION_PICKERS", ())
    for index, arg in enumerate(argv):
        key, _, inline = arg.partition("=")
        if key not in pickers:
            continue
        value = inline if inline else (argv[index + 1] if index + 1 < len(argv) else "")
        # A picker with nothing after it, or followed by the next flag: the
        # session is chosen inside the harness, out of our sight.
        return (value if SESSION_ID.match(value) else None), argv
    chosen = str(uuid.uuid4())
    return chosen, [flag[0], flag[1] % chosen, *argv]


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

    every = globmodule.glob(pattern % fields)
    # Born during this run, rather than merely touched by it. A session file is
    # created once and written to for as long as the conversation lasts, so on a
    # machine where a dozen windows share one directory, "created since the box
    # started" identifies this box's own session and "modified" identifies
    # whoever typed last. One harness keeps five hundred of these in a single
    # directory, with nothing separating projects at all.
    born = [f for f in every if _created(f) >= since]
    if len(born) == 1:
        return _session_id(born[0])
    found = [f for f in every if os.path.getmtime(f) >= since]
    if not found:
        return None
    # More than one session was written while this box ran, so the newest is
    # only a guess — and on this machine a wrong guess is the normal case:
    # every window open on the same project files its sessions in the same
    # directory, and they all write constantly. A line that names a stranger's
    # conversation is worse than no line: it looks exactly like the right
    # answer. Say nothing instead, and let the harness's own line stand.
    if len(found) > 1:
        return None
    return _session_id(max(found, key=lambda f: os.path.getmtime(f)))


def _created(path):
    """When this file came into being, where the filesystem records it."""
    stat = os.stat(path)
    return getattr(stat, "st_birthtime", stat.st_ctime)


def _session_id(path):
    """The id a harness gave this session, however it spells the filename."""
    stem = os.path.splitext(os.path.basename(path))[0]
    # Some name the file after the session; others prefix it with a timestamp
    # and leave the id at the end.
    match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", stem)
    return match.group(0) if match else stem


def _session_root(provider, config_dir):
    """Where this harness keeps its sessions, with links resolved."""
    pattern = getattr(provider, "SESSION_GLOB", "") or ""
    head = pattern.split("*", 1)[0] % {
        "config": config_dir, "home": home_dir(), "key": "",
    }
    return os.path.realpath(head)


def _sessions_are_shared(provider, env=None):
    """Whether a session can be reopened without naming the account.

    The account only matters when sessions live INSIDE the profile: the broker
    moves to another account when one runs out of room, and an id recorded
    under the first is then not found at all. When every profile reaches one
    pile — a link, or simply the same directory — naming the account adds
    nothing, and the broker choosing an account for itself is the point of it.
    """
    home_env = getattr(provider, "HOME_ENV", None)
    config = (env or {}).get(home_env) if home_env and home_env != "HOME" else None
    if not config:
        return True
    canonical = expand(getattr(provider, "CANONICAL_HOME", "~"))
    return _session_root(provider, expand(config)) == _session_root(provider, canonical)


def _resume_hint(provider, name, workdir, env=None, since=0, account=None, session=None):
    """What to type to come back INTO this box, on the same account.

    The harness prints its own resume line as it exits, and that line is missing
    the box: run it as printed and the session reopens on the host, in a
    different world, which is not obvious until something behaves oddly.

    The account is named only when it would otherwise be lost: a harness that
    files its sessions INSIDE the per-account profile records an id the next
    account cannot find ("no rollout found for thread id"). When the profiles
    all reach one pile of sessions — which is the normal arrangement — the
    broker picks an account by itself and the line stays clean.
    """
    session = session or _last_session(provider, workdir, env, since)
    # Not knowing which session this was is not a reason to leave someone
    # without the box. The harness prints its own id as it exits, one line
    # above this one; point at that rather than invent one.
    resume = (getattr(provider, "SESSION_RESUME", "--resume %s")
              % (session or "<the id printed above>"))
    pin = ""
    if (account and getattr(provider, "CREDENTIALS", "file") != "env"
            and not _sessions_are_shared(provider, env)):
        pin = "%s_ACCOUNT=%s " % (provider.NAME.upper(), account)
    # The box goes last, after the id, so the line differs from the one the
    # harness printed above it only by a suffix: type that suffix onto the end
    # of what you already have, or delete it to go back to the host.
    return "\nResume it in this box with:\n  %s%s %s --box %s\n" % (pin, provider.BIN, resume, name)


SYNC_LOG = os.path.join(config.CONFIG_DIR, "box", "sync.log")


def _sync_back(provider, session, env=None):
    """Put what the box wrote back into the history out here — in the background.

    A box works on its own clone of the harness's databases, because sharing one
    SQLite file across the container boundary tears it. The work itself is in
    the session file, which IS shared — so the harness is asked to read that
    session back into its history, which is what the command exists for. Merging
    its tables by hand would be us guessing at someone else's schema.

    Waited for, this held the prompt for several seconds every time: the harness
    looks through every session it has to find the one named, and there are
    thirteen thousand of them here. Nothing downstream depends on it having
    finished — the session file is the record, the database is a view of it — so
    it is started and left to run, with its output kept in case it fails.
    """
    template = getattr(provider, "BOX_SYNC", None)
    if not template:
        return
    from ..run import real_bin

    binary = real_bin(provider)
    if not binary:
        return
    # One command, or several to run in order — a harness may need more than a
    # single call to take a session into its history.
    steps = template if isinstance(template[0], (list, tuple)) else (template,)
    argvs = [[binary] + [part % {"session": session} for part in step] for step in steps]
    by_hand = " && ".join(
        " ".join([provider.BIN] + [part % {"session": session} for part in step])
        for step in steps)
    try:
        os.makedirs(os.path.dirname(SYNC_LOG), mode=0o700, exist_ok=True)
        log = open(SYNC_LOG, "a")
    except OSError:
        log = subprocess.DEVNULL
    try:
        log.write("\n=== %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), by_hand))
        log.flush()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        # Its own session, so quitting the terminal does not take it with it.
        # Chained through a shell rather than started one by one, because
        # nothing here waits: the steps must still run in order after this
        # process is gone.
        import shlex

        script = " && ".join(" ".join(shlex.quote(a) for a in argv) for argv in argvs)
        subprocess.Popen(["/bin/sh", "-c", script], env={**os.environ, **(env or {})},
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # Not fatal: the session file is intact and the command can be run again
        # by hand. Say which one, so it can be.
        warn("could not fold this session back into the history (%s) — run: %s"
             % (exc, by_hand))
    finally:
        if log is not subprocess.DEVNULL:
            try:
                log.close()
            except OSError:
                pass


# Putting the terminal back the way the harness found it.
#
# A harness in a box owns the terminal completely: it switches to the alternate
# screen, asks for mouse reports, and turns on the kitty keyboard protocol, in
# which Enter arrives as "27;3u" and an arrow as "1:1A" rather than as ordinary
# characters. On the way out it undoes every one of those — but only if it gets
# to run. Stop the container from outside, or let it crash, and the process dies
# where it stands: the pane is left speaking a language the shell underneath
# does not understand, and typing into it produces "zsh: command not found: 1:1A".
#
# So the undoing belongs out here, in the thing that outlives the container.
# Sending these when they are already off costs nothing — each is a no-op on a
# terminal that is already in that state.
TERMINAL_RESET = (
    "\033[?1049l"                  # leave the alternate screen
    "\033[<u"                      # pop the kitty keyboard flags
    "\033[=0;1u"                   # ...and clear any that were set outright
    "\033[?1l\033>"                # cursor keys and keypad back to normal
    "\033[?2004l"                  # bracketed paste off
    "\033[?1004l"                  # focus in/out reporting off — it arrives as "\033[O"
    "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"  # mouse reporting off
    "\033[?25h"                    # cursor visible again
    "\033[0m"                      # attributes back to default
)


def _terminal_state():
    """This terminal's driver settings, to be restored after the run."""
    try:
        if not sys.stdin.isatty():
            return None
        return termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, OSError, ValueError):
        return None


def _restore_terminal(saved):
    """Undo both halves of what a harness does to a terminal: the driver's
    settings (raw mode, no echo) and the modes held by the emulator itself."""
    if saved is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        except (termios.error, OSError, ValueError):
            pass
    try:
        if sys.stdout.isatty():
            sys.stdout.write(TERMINAL_RESET)
            sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _guard_terminal(saved):
    """Restore the terminal on the signals that would otherwise skip `finally`.

    A `finally` covers the ordinary endings — the harness exits, the container
    crashes, docker fails to start one. It does not cover this process being
    told to end: the default action for SIGTERM and SIGHUP is to die on the
    spot, leaving the pane in the harness's modes. SIGKILL still cannot be
    caught, and that is the one case left for `broker box repair`.
    """
    def handler(number, _frame):
        _restore_terminal(saved)
        # Exit the way the signal would have, so anything waiting on this
        # process still sees a signal death rather than a plain status.
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)

    for number in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):  # not the main thread, or no such signal
            pass


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
    pinned, argv = _pin_session(provider, argv)
    cmd = command(provider, name, defined[name], argv, env)
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
    try:
        finished = subprocess.run(cmd)
        status = finished.returncode
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

    session = pinned or _last_session(provider, workdir, env, started)
    if session:
        # Printed BEFORE the session is folded back, and not only after a clean
        # exit. Folding asks the harness to re-read its own session, and it
        # looks through every session it has to find the one named — thirteen
        # thousand of them here, which is seconds of silence. The line is what
        # the person is waiting for; the bookkeeping can happen behind it.
        hint = _resume_hint(provider, name, workdir, env, started, account, session)
        if hint:
            sys.stdout.write(hint)
            sys.stdout.flush()
        _sync_back(provider, session, env)
    sys.exit(status)
