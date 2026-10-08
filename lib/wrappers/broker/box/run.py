"""The container command line: every flag that makes a box a box."""

import os
import re
import shutil
import sys

from ..out import die, warn
# Imported as modules, not as names: a test that replaces one of these replaces
# it where it lives, and a bound name here would keep pointing at the original.
from . import clones, environ, extras, harness, http_mcp, mcp, nested, shim
from .mcp import HOST_GATEWAY
from .paths import _bind, _mount, _paths, expand, home_dir


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
    cmd += nested.flags(name, profile, binary)
    cmd += environ.carried(profile, env, home, name)
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
    cmd += extras.tools(binary, image, name, profile, home)

    (own, http_config, http_config_mounted, http_mode,
     settings_mounted) = harness.own_home(provider, profile, env, name, mounted)
    cmd += own
    cmd += extras.databases(provider, env)
    cmd += extras.gitlab(profile, env, name, home)
    cmd += clones.blank(provider, name, mounted)
    cmd += clones.readonly(provider, mounted)

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

    cmd += extras.own_copies(provider, profile, name)
    cmd += clones.private(provider, profile, env, name)

    canonical = expand(getattr(provider, "CANONICAL_HOME", "~"))
    cmd += harness.siblings(provider, canonical)

    cmd += harness.chosen_home(provider, env, name, remote, mounted, canonical)
    if http_config and not http_config_mounted:
        cmd += _bind(http_config[0], http_config[2], http_mode)

    cmd += harness.settings(provider, env, canonical, http_config, settings_mounted)
    cmd += environ.credentials(provider, env)

    # MCP servers that exist only on this machine. Half of them cannot come
    # along at all — some are native macOS binaries — so the server
    # stays on the host and only its stdio is carried across. The shim is
    # mounted AT THE COMMAND'S OWN PATH, which means the harness's own config
    # needs no rewriting: it already points there. Servers reached over http are
    # left alone; the box can dial them itself.
    inherited = {}
    if profile.get("mcp") is not False:
        claimed = {}
        for server_name, server in sorted(mcp.mcp_servers(provider, env).items()):
            target = server["command"][0]
            if target in claimed:
                warn("%s and %s start from the same command (%s) — only the first is bridged"
                     % (claimed[target], server_name, target))
                continue
            live = mcp._start_bridge(server_name, server, profile, projects)
            if not live:
                continue
            for variable in server.get("inherit") or []:
                if os.environ.get(variable) and variable not in inherited:
                    inherited[variable] = os.environ[variable]
            claimed[target] = server_name
            cmd += _bind(shim._write_shim(provider, server_name, live), target, "ro")
        for variable, value in sorted(inherited.items()):
            cmd += ["-e", "%s=%s" % (variable, value)]
        if claimed:
            # Docker Desktop resolves this name already; Colima and plain Linux
            # need to be told, and saying it twice costs nothing.
            cmd += ["--add-host", "%s:host-gateway" % HOST_GATEWAY]

    cmd += environ.own(profile)

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
