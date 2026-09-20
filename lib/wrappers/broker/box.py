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
"""

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time

from . import config
from .out import die, warn

# Set by the engine before it hands a harness a profile through $HOME.
REAL_HOME_ENV = "BROKER_REAL_HOME"


def home_dir():
    """Your home — the one you had before a profile took over $HOME.

    A provider whose credentials live in a FILE gives the harness a profile
    directory as $HOME, so by the time a box is built "~" no longer means what
    it says. Expanding it then put ~/.gemini inside the profile itself: the real
    one was never mounted, every symlink the profile makes back into it dangled,
    and agy refused to start over a file that was there all along.

    $HOME still wins when nothing has been substituted — a container, a test or
    anything else that sets it means it.
    """
    return os.environ.get(REAL_HOME_ENV) or os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir


def expand(path):
    """expanduser(), but against the real home rather than the current $HOME."""
    path = str(path)
    if path == "~":
        return home_dir()
    if path.startswith("~" + os.sep):
        return os.path.join(home_dir(), path[2:])
    return os.path.expanduser(path)


# Inside a container "localhost" is the container. This is the host.
HOST_GATEWAY = "host.docker.internal"

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


def mcp_servers(provider, env=None):
    """The MCP servers this harness declares that a box would otherwise lose.

    Only the ones started as a COMMAND: a server reached over http needs nothing
    from us, it is already reachable from inside the box.

    Read from the profile this run actually uses, not from the canonical home.
    An account's profile carries its own copy of the config, and the two drift:
    here one spelled a server's path through ~/Projects and the other through
    /Volumes, so the box mounted the stand-in where the harness never looked and
    reported the server missing.
    """
    spec = getattr(provider, "MCP_CONFIG", None)
    if not spec:
        return {}
    path, kind, key = spec
    path = expand(path)

    home_env = getattr(provider, "HOME_ENV", None)
    canonical = getattr(provider, "CANONICAL_HOME", None)
    in_use = (env or {}).get(home_env) if home_env else None
    if in_use and canonical:
        in_use = os.path.abspath(expand(in_use))
        canonical = os.path.abspath(expand(canonical))
        if path.startswith(canonical + os.sep):
            path = os.path.join(in_use, os.path.relpath(path, canonical))
    try:
        if kind == "toml":
            import tomllib

            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        else:
            with open(path) as fh:
                data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        warn("could not read %s for MCP servers: %s" % (path, exc))
        return {}
    found = {}
    for name, server in (data.get(key) or {}).items():
        command = server.get("command")
        if not command:
            continue  # http/sse: the box reaches it over the network
        found[name] = {
            "command": [command] + list(server.get("args") or []),
            "env": dict(server.get("env") or {}),
            # codex declares which variables a server expects to inherit rather
            # than spelling out their values. Inside a box nothing is inherited,
            # so the server starts without them — and one that needs them to
            # know who it is answering for simply exposes no tools, which reads
            # as "server missing" and is nothing of the kind.
            "inherit": [v for v in (server.get("env_vars") or []) if isinstance(v, str)],
        }
    return found


def _bridge_env(name, server, profile, projects):
    """The environment the host-side server runs with, for THIS box.

    A server that answers questions about code must answer about the code this
    box is for. So what the box says wins, and where it says nothing the box's
    own project is the default — a root still pointing at the whole machine
    would let one box ask about code it cannot even see.
    """
    env = dict(os.environ)
    env.update(server.get("env") or {})
    override = ((profile.get("mcp") or {}).get(name) or {}).get("env") or {}
    for key, value in override.items():
        value = expand(str(value))
        # A value that IS a path becomes the physical one. Writing
        # ~/Projects/foo where that is a symlink would otherwise key a
        # code-memory database under a second name and reindex from scratch —
        # a trap you would have to remember every time you edited the file.
        if value.startswith("/") and os.path.exists(value):
            value = os.path.realpath(value)
        env[key] = value
    # The physical path, not the one you typed: this server keys its per-project
    # database off the path it is given, so the symlinked spelling would start a
    # second database and reindex the whole project from scratch.
    if projects and "CBM_ALLOWED_ROOT" in env and "CBM_ALLOWED_ROOT" not in override:
        env["CBM_ALLOWED_ROOT"] = os.path.realpath(projects[0])
    return env


def _start_bridge(name, server, profile, projects):
    """Make sure a listener for this server is up, and say how to reach it."""
    from . import mcpbridge

    # Same server, same caller identity — anything else gets its own listener.
    key = mcpbridge.identity_key(
        extra=((profile.get("mcp") or {}).get(name) or {}).get("identity_env") or ())
    live = mcpbridge.running(name, key)
    if live and live.get("command") == server["command"]:
        return live
    try:
        subprocess.Popen(
            [sys.executable, "-m", "broker.mcpbridge", "serve", name, "--"] + server["command"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=_bridge_env(name, server, profile, projects),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # it outlives this process: the box is its client
        )
    except OSError as exc:
        warn("could not bridge the %s MCP server: %s" % (name, exc))
        return None
    for _ in range(50):
        live = mcpbridge.running(name, key)
        if live:
            return live
        time.sleep(0.1)
    warn("the %s MCP bridge did not come up — it will be missing inside the box" % name)
    return None


# The shim carries its own client, so the image has to know nothing about any of
# this — it only needs python3, which a harness image has anyway.
SHIM_TEMPLATE = "\n".join([
    "#!/usr/bin/env python3",
    "# Written by the broker: %(name)s runs on the host; this is the wire to it.",
    "import socket, sys, threading",
    "HOST, PORT, TOKEN = %(host)r, %(port)d, %(token)r",
    "",
    "",
    "def pump(src, dst, done=None):",
    "    try:",
    "        while True:",
    "            if hasattr(src, 'recv'):",
    "                chunk = src.recv(65536)",
    "            else:",
    "                # read1: read() would block for a full buffer and deadlock",
    "                chunk = src.read1(65536) if hasattr(src, 'read1') else src.read(65536)",
    "            if not chunk:",
    "                break",
    "            if hasattr(dst, 'sendall'):",
    "                dst.sendall(chunk)",
    "            else:",
    "                dst.write(chunk)",
    "                dst.flush()",
    "    except (OSError, ValueError):",
    "        pass",
    "    finally:",
    "        if done:",
    "            try:",
    "                done()",
    "            except OSError:",
    "                pass",
    "",
    "",
    "with socket.create_connection((HOST, PORT)) as conn:",
    "    conn.sendall(TOKEN.encode() + b'\\n')",
    "    out = threading.Thread(target=pump, args=(conn, sys.stdout.buffer), daemon=True)",
    "    out.start()",
    "    pump(sys.stdin.buffer, conn, lambda: conn.shutdown(socket.SHUT_WR))",
    "    out.join(timeout=5)",
    "",
])


def _write_shim(provider, name, live):
    """A stand-in for the server's command, to be mounted at its own path."""
    directory = os.path.join(config.CONFIG_DIR, "box", "shims", provider.NAME)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, name.replace("/", "_"))
    body = SHIM_TEMPLATE % {"name": name, "host": HOST_GATEWAY,
                            "port": live["port"], "token": live["token"]}
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.chmod(tmp, 0o700)  # it carries the connection secret
    os.replace(tmp, path)
    return path


def _paths(profile, key):
    """Each path a box lists, as (what to mount, where it lands inside).

    Usually those are the same — a project keeps its own path, which is what
    makes session history and --resume work. They differ when a box needs its
    OWN copy of something the host also has: a directory a shared tool insists
    on writing to, where two boxes writing into one place would mix their state.
    Then the box names where it really is and where the tool expects it.
    """
    for entry in profile.get(key) or []:
        if isinstance(entry, dict):
            source = os.path.abspath(expand(entry.get("source") or ""))
            target = os.path.abspath(expand(entry.get("target") or source))
            if not entry.get("source"):
                die("a path in the box lists no source: %r" % (entry,))
            # Its own, so it has to exist before the box can start.
            if not os.path.exists(source):
                os.makedirs(source, mode=0o700, exist_ok=True)
            yield source, target
        else:
            path = os.path.abspath(expand(entry))
            yield path, path


def _ssh_config(name, profile):
    """A private ~/.ssh for this box: only the keys it was told about.

    Mounting the real ~/.ssh hands a box every key on the machine — GitHub, the
    cloud VMs, whatever else is in there — when what it usually needs is one
    host. Naming keys in the box keeps the rest out of reach entirely: they are
    not mounted, so nothing inside can use them however it is asked to.

    The generated config pins each host to its key with IdentitiesOnly, because
    the tools that need this call plain `ssh <host>` with no -i of their own.
    """
    spec = profile.get("ssh")
    if not isinstance(spec, dict):
        return None, [], None
    keys = [expand(k) for k in (spec.get("keys") or [])]

    # A host is either just its key, or a small table when the name you use is
    # not the address: your own ~/.ssh/config does not come along, so an alias
    # that resolves on the host resolves to nothing in here.
    hosts = {}
    for host, entry in (spec.get("hosts") or {}).items():
        if isinstance(entry, dict):
            settings = {k: v for k, v in entry.items() if k != "key"}
            hosts[host] = (expand(entry.get("key") or ""), settings)
        else:
            hosts[host] = (expand(entry), {})
    keys += [k for k, _ in hosts.values() if k and k not in keys]
    missing = [k for k in keys if k and not os.path.exists(k)]
    if missing:
        die("the '%s' box names ssh keys that do not exist: %s" % (name, ", ".join(missing)))
    if not keys:
        return None, [], None

    # ssh reads these in the order it finds them, and the spelling is its own:
    # HostName, User, Port, ProxyJump. Anything else the box names is passed
    # through as written rather than guessed at.
    ORDER = ("hostname", "user", "port", "proxyjump")
    SPELLING = {"hostname": "HostName", "user": "User", "port": "Port", "proxyjump": "ProxyJump"}
    lines = ["# Written by the broker for the '%s' box." % name]
    for host, (key, settings) in sorted(hosts.items()):
        lines.append("Host %s" % host)
        for field in ORDER:
            if settings.get(field) is not None:
                lines.append("  %s %s" % (SPELLING[field], settings[field]))
        for field, value in sorted(settings.items()):
            if field not in ORDER and value is not None:
                lines.append("  %s %s" % (field, value))
        if key:
            lines += ["  IdentityFile %s" % key, "  IdentitiesOnly yes"]
        lines.append("")
    # The hosts it will talk to, and only those. Without a known_hosts the box
    # cannot verify anything and cannot write what it learns either — the
    # directory it would write into belongs to the container. Copying the whole
    # host file instead would tell the box about every machine you have ever
    # reached, which is not access but is not its business either.
    known = []
    for host, (_, settings) in sorted(hosts.items()):
        # Look up what ssh will actually connect to, not the name you call it by.
        lookup = settings.get("hostname") or host
        try:
            found = subprocess.run(["ssh-keygen", "-F", str(lookup)], capture_output=True, text=True, timeout=15)
            known += [l for l in found.stdout.splitlines() if l and not l.startswith("#")]
        except (OSError, subprocess.SubprocessError):
            pass

    directory = os.path.join(config.CONFIG_DIR, "box")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "ssh-config-%s" % name.replace("/", "_"))
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    hosts_file = None
    if known:
        hosts_file = os.path.join(directory, "ssh-known-hosts-%s" % name.replace("/", "_"))
        tmp = hosts_file + ".new"
        with open(tmp, "w") as fh:
            fh.write("\n".join(known) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, hosts_file)
    return path, keys, hosts_file


def _passwd_file(runtime, image):
    """An /etc/passwd that knows who you are inside the box.

    The box runs as your own uid, which no image has an account for — and some
    tools refuse to start without one: ssh dies with "No user exists for uid
    501" before it reads a single option, which takes the Windows test offload
    with it. So the image's own passwd gets one line appended and is mounted
    back over itself. Built once and cached; the file it is built from changes
    about as often as the image is rebuilt.
    """
    cache = os.path.join(config.CONFIG_DIR, "box", "passwd-%s" % image.replace("/", "_").replace(":", "_"))
    if os.path.exists(cache):
        return cache
    try:
        base = subprocess.run([runtime, "run", "--rm", "--entrypoint", "cat", image, "/etc/passwd"],
                              capture_output=True, text=True, timeout=120)
        if base.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    user = os.environ.get("USER") or "user"
    line = "%s:x:%d:%d::%s:/bin/bash\n" % (user, os.getuid(), os.getgid(), home_dir())
    try:
        os.makedirs(os.path.dirname(cache), mode=0o700, exist_ok=True)
        tmp = cache + ".new"
        with open(tmp, "w") as fh:
            fh.write(base.stdout if base.stdout.endswith("\n") else base.stdout + "\n")
            fh.write(line)
        os.chmod(tmp, 0o644)
        os.replace(tmp, cache)
    except OSError:
        return None
    return cache


def _mount(host, mode="rw", target=None):
    """Mount a path at its own path — and at its physical one too, if they differ.

    ~/Projects/foo is often a symlink to /Volumes/.../foo. Inside the box only
    the name you gave would exist, and that breaks things that resolve symlinks
    on the host: a bridged MCP server answers with /Volumes/... paths, and the
    harness inside cannot open a single one of them. Mounting both costs one
    more bind of the same source.
    """
    flag = ",readonly" if mode == "ro" else ""
    target = target or host
    args = ["--mount", "type=bind,source=%s,target=%s%s" % (host, target, flag)]
    # Only when the path is kept as-is: a redirected mount is already somewhere
    # else on purpose, and its physical twin would land on top of the original.
    physical = os.path.realpath(host)
    if target == host and physical != host:
        args += ["--mount", "type=bind,source=%s,target=%s%s" % (physical, physical, flag)]
    return args


def _empty_file():
    """A file to cover a credentials path with, so the box cannot read one."""
    blank = os.path.join(config.CACHE_DIR, "box-empty")
    os.makedirs(os.path.dirname(blank), mode=0o700, exist_ok=True)
    if not os.path.exists(blank):
        with open(blank, "w"):
            pass
        os.chmod(blank, 0o600)
    return blank


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
                "--mount", "type=volume,source=broker-box-docker-%s,target=/var/lib/docker" % name]
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

    mounted = []
    passwd = _passwd_file(binary, image)
    if passwd:
        cmd += ["--mount", "type=bind,source=%s,target=/etc/passwd,readonly" % passwd]

    for entry in COMMON_RO:
        host = expand(entry)
        if os.path.exists(host):
            cmd += _mount(host, "ro")

    # Named keys only, at their own paths, so a tool that resolves ~/.ssh/<name>
    # finds what it expects and nothing else is there to find.
    ssh_config, ssh_keys, ssh_known = _ssh_config(name, profile)
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
        for name, server in sorted(mcp_servers(provider, env).items()):
            target = server["command"][0]
            if target in claimed:
                warn("%s and %s start from the same command (%s) — only the first is bridged"
                     % (claimed[target], name, target))
                continue
            live = _start_bridge(name, server, profile, projects)
            if not live:
                continue
            for variable in server.get("inherit") or []:
                if os.environ.get(variable) and variable not in inherited:
                    inherited[variable] = os.environ[variable]
            claimed[target] = name
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly"
                    % (_write_shim(provider, name, live), target)]
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


def exec_box(provider, name, argv, env):
    """Become the container. Nothing runs on the host after this."""
    defined = profiles()
    if name not in defined:
        die("no box called '%s' in %s%s" % (
            name, PATH,
            (" — defined: " + ", ".join(sorted(defined))) if defined else " (the file does not exist yet)"))
    # A terminal manager watches the pane's foreground process to know what runs
    # in it. From here on that process is `docker`, which hides the harness
    # behind it — Herdr documents the way out: a wrapper says which agent it
    # stands for, and the manager reads its screen as it would any other.
    if os.environ.get("HERDR_PANE_ID") and not os.environ.get("HERDR_AGENT"):
        os.environ["HERDR_AGENT"] = provider.NAME

    cmd = command(provider, name, defined[name], argv, env)
    try:
        os.execv(cmd[0], cmd)
    except OSError as exc:
        die("cannot start the '%s' box: %s" % (name, exc))
