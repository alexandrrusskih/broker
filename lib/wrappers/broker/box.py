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
import shutil
import subprocess
import sys
import time

from . import config
from .out import die, warn

# Inside a container "localhost" is the container. This is the host.
HOST_GATEWAY = "host.docker.internal"

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


def mcp_servers(provider):
    """The MCP servers this harness declares that a box would otherwise lose.

    Only the ones started as a COMMAND: a server reached over http needs nothing
    from us, it is already reachable from inside the box.
    """
    spec = getattr(provider, "MCP_CONFIG", None)
    if not spec:
        return {}
    path, kind, key = spec
    path = os.path.expanduser(path)
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
        value = os.path.expanduser(str(value))
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

    live = mcpbridge.running(name)
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
        live = mcpbridge.running(name)
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
    for entry in profile.get(key) or []:
        yield os.path.abspath(os.path.expanduser(entry))


def _mount(host, mode="rw"):
    """Mount a path at its own path — and at its physical one too, if they differ.

    ~/Projects/foo is often a symlink to /Volumes/.../foo. Inside the box only
    the name you gave would exist, and that breaks things that resolve symlinks
    on the host: a bridged MCP server answers with /Volumes/... paths, and the
    harness inside cannot open a single one of them. Mounting both costs one
    more bind of the same source.
    """
    flag = ",readonly" if mode == "ro" else ""
    args = ["--mount", "type=bind,source=%s,target=%s%s" % (host, host, flag)]
    physical = os.path.realpath(host)
    if physical != host:
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

    home = os.path.expanduser("~")
    image = profile.get("image") or "broker-box"

    cmd = [binary, "run", "--rm", "--init"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        cmd.append("-it")
    # The harness writes as you, not as root: files it creates in the project
    # stay yours, and nothing needs chown afterwards.
    cmd += ["--user", "%d:%d" % (os.getuid(), os.getgid())]
    cmd += ["-e", "HOME=%s" % home, "-e", "USER=%s" % (os.environ.get("USER") or "user")]
    # $HOME itself is a tmpfs owned by that uid. Without it the harness cannot
    # write to its own home: the container creates missing mount points as root,
    # and mounting the real home instead would hand the box everything in it.
    # The directories below land on top of this, so what is mounted survives and
    # what is not is discarded with the container.
    cmd += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700" % (home, os.getuid(), os.getgid())]

    mounted = []
    # The harness's own directory: settings, MCP servers, agents, history.
    for entry in getattr(provider, "BOX_HOME", ()):
        host = os.path.expanduser(entry)
        if os.path.exists(host):
            cmd += _mount(host)
            mounted.append(host)
    # ...minus its credentials file. The token comes from the broker below.
    blank = None
    for entry in getattr(provider, "BOX_SECRETS", ()):
        host = os.path.expanduser(entry)
        if any(host.startswith(m + os.sep) for m in mounted):
            blank = blank or _empty_file()
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly" % (blank, host)]

    projects = list(_paths(profile, "rw"))
    for host in projects:
        if not os.path.isdir(host):
            die("the '%s' box lists %s, which does not exist" % (name, host))
        cmd += _mount(host)
    for host in _paths(profile, "ro"):
        if not os.path.isdir(host):
            die("the '%s' box lists %s, which does not exist" % (name, host))
        cmd += _mount(host, "ro")

    # Start where you started, when that is inside the box; otherwise in the
    # first writable project, so a bare `claude --box work` lands somewhere real.
    cwd = os.getcwd()
    inside = any(cwd == p or cwd.startswith(p + os.sep) for p in projects)
    cmd += ["-w", cwd if inside else (projects[0] if projects else home)]

    # A provider that reads its credentials from a FILE has them in a profile
    # directory; that directory comes in at its own path, and the variable
    # pointing at it comes with it.
    home_env = getattr(provider, "HOME_ENV", None)
    if getattr(provider, "CREDENTIALS", "file") != "env" and home_env and env.get(home_env):
        host = os.path.abspath(os.path.expanduser(env[home_env]))
        if os.path.isdir(host) and host not in mounted:
            cmd += _mount(host)
            cmd += ["-e", "%s=%s" % (home_env, host)]

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
    # along at all — ntk and codebase-memory are macOS binaries — so the server
    # stays on the host and only its stdio is carried across. The shim is
    # mounted AT THE COMMAND'S OWN PATH, which means the harness's own config
    # needs no rewriting: it already points there. Servers reached over http are
    # left alone; the box can dial them itself.
    if profile.get("mcp") is not False:
        claimed = {}
        for name, server in sorted(mcp_servers(provider).items()):
            target = server["command"][0]
            if target in claimed:
                warn("%s and %s start from the same command (%s) — only the first is bridged"
                     % (claimed[target], name, target))
                continue
            live = _start_bridge(name, server, profile, projects)
            if not live:
                continue
            claimed[target] = name
            cmd += ["--mount", "type=bind,source=%s,target=%s,readonly"
                    % (_write_shim(provider, name, live), target)]
        if claimed:
            # Docker Desktop resolves this name already; Colima and plain Linux
            # need to be told, and saying it twice costs nothing.
            cmd += ["--add-host", "%s:host-gateway" % HOST_GATEWAY]

    cmd.append(image)
    cmd.append(provider.BIN)
    cmd += argv
    return cmd


def exec_box(provider, name, argv, env):
    """Become the container. Nothing runs on the host after this."""
    defined = profiles()
    if name not in defined:
        die("no box called '%s' in %s%s" % (
            name, PATH,
            (" — defined: " + ", ".join(sorted(defined))) if defined else " (the file does not exist yet)"))
    cmd = command(provider, name, defined[name], argv, env)
    try:
        os.execv(cmd[0], cmd)
    except OSError as exc:
        die("cannot start the '%s' box: %s" % (name, exc))
