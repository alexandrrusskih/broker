"""The container command line: every flag that makes a box a box."""

import os
import re
import shutil
import sys

from ..out import die, warn
# Imported as modules, not as names: a test that replaces one of these replaces
# it where it lives, and a bound name here would keep pointing at the original.
from . import clones, environ, extras, harness, herdr, http_mcp, mcp, nested, report, shim
from .mcp import HOST_GATEWAY
from .paths import _bind, _mount, _paths, expand, home_dir


def _must_exist(name, host):
    """Refuse a listed path that is not there, and say which it is.

    Directories are the ordinary case, but not the only one a box has a use
    for: a single file, and a unix socket — the terminal manager's, for a box
    that is meant to reach it. The check was os.path.isdir, so both were
    refused with "does not exist" about a path that plainly did, which sends
    whoever reads it looking for the wrong thing.

    A file bound in this way carries the warning the harness home already
    carries: a bind mount holds the inode it was given, so a file rewritten
    atomically out here goes stale inside. For a socket that means a box
    started before the server restarted is talking to nothing.
    """
    if os.path.exists(host):
        return
    die("the '%s' box lists %s, which does not exist" % (name, host))


def command(provider, name, profile, argv, env, remote=False, companions=()):
    """The full container command line for this run.

    `companions` are the other harnesses the box carries (box/companions.py),
    each with the environment its own account was installed into.
    """
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
    # Every other directory a mount point needs above it — ~/.bun for a bun
    # cache at ~/.bun/install/cache, ~/.cache for a session database below it —
    # is derived from the mounts themselves at the end of this function. See
    # _parents: named by hand, one of them is always the one nobody named yet.

    mounted = []
    cmd += extras.tools(binary, image, name, profile, home)
    cmd += extras.gitlab(profile, env, name, home)
    # The terminal manager, for a box that was told it may reach it. Its own
    # socket cannot cross into a container on this platform, so the command
    # crosses instead — see box/herdr.py.
    cmd += herdr.mount(provider, profile, name)

    writable = list(_paths(profile, "rw"))
    for host, target in writable:
        _must_exist(name, host)
        cmd += _mount(host, "rw", target)
    for host, target in _paths(profile, "ro"):
        _must_exist(name, host)
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

    inherited, claimed = {}, {}
    cmd += _harness(provider, profile, env, name, remote, mounted, projects, claimed, inherited)
    # The other harnesses this box carries, each set up exactly as it would be
    # as the box's own. Two of them may want the same thing at the same place —
    # the same MCP server, the same writable cache — and docker refuses a
    # container with a mount point named twice, so the second asking is dropped.
    for other, other_env in companions:
        cmd += environ.companion(env, other_env)
        cmd += _unclaimed(cmd, _harness(other, profile, other_env, name, remote, mounted,
                                        projects, claimed, inherited))

    for variable, value in sorted(inherited.items()):
        cmd += ["-e", "%s=%s" % (variable, value)]
    if claimed:
        # Docker Desktop resolves this name already; Colima and plain Linux
        # need to be told, and saying it twice costs nothing.
        cmd += ["--add-host", "%s:host-gateway" % HOST_GATEWAY]

    cmd += environ.own(profile)

    # The one place a box may say which chat is in it. See box/report.py: the
    # directory is its whole authority, and the pane is never in it.
    #
    # AFTER environ.own, which is the box's own settings and documented to win
    # over everything above it. Before it, a box could name
    # AGNTBUS_SESSION_REPORT_DIR in its own "env" and send its reports
    # somewhere else — or nowhere — and this is the one variable a box must not
    # be able to choose. Docker takes the LAST -e for a repeated name, so this
    # is the last word.
    cmd += report.flags(name)

    # ...and the directories the mounts above would otherwise have the container
    # create for itself. Last, so it sees every mount any part of this made.
    cmd += _parents(cmd, home)

    cmd.append(image)
    cmd.append("broker-box-entry")
    cmd.append(provider.BIN)
    if profile.get("mcp") is not False:
        cmd += http_mcp.codex_args(provider, env)

    # Flags the box hands the harness, before what you typed — so a flag you
    # pass on the command line still wins. First the engine's own defaults
    # (provider BOX_ARGS): a broker box is the externally sandboxed environment
    # the danger flags describe — its boundary is what it mounts, and a
    # permission prompt inside it only asks the container. Then the box's own
    # "args" on top of those, which may still tighten or widen per harness:
    #
    #   "args": { "codex": ["--full-auto"], "claude": ["--permission-mode", "plan"] }
    #
    # A plain list applies to every harness in the box.
    extra = profile.get("args")
    if isinstance(extra, dict):
        extra = extra.get(provider.NAME) or []
    flags = [str(flag) for flag in getattr(provider, "BOX_ARGS", ())]
    flags += [str(flag) for flag in extra or []]
    added = set(argv)
    for flag in flags:
        if flag not in added:
            cmd.append(flag)
            added.add(flag)

    cmd += argv
    return cmd


def _harness(provider, profile, env, name, remote, mounted, projects, claimed, inherited):
    """Everything one harness needs in the box: its home, its account, its MCP."""
    args = []
    # ...and anywhere else this harness insists on writing. Mounting a file
    # deep under $HOME makes the container create its parents as root, and the
    # harness — which runs as you — then cannot make a sibling directory next
    # to its own database: "EACCES: permission denied, mkdir". Naming the
    # directory here gets it owned by you before anything is mounted into it.
    for directory in getattr(provider, "BOX_WRITABLE", ()):
        args += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700,exec"
                 % (expand(directory), os.getuid(), os.getgid())]

    (own, http_config, http_config_mounted, http_mode,
     settings_mounted) = harness.own_home(provider, profile, env, name, mounted)
    args += own
    args += extras.databases(provider, env)
    args += clones.blank(provider, name, mounted)
    args += clones.readonly(provider, mounted)
    args += clones.private(provider, profile, env, name)

    canonical = expand(getattr(provider, "CANONICAL_HOME", "~"))
    args += harness.siblings(provider, canonical)

    args += harness.chosen_home(provider, env, name, remote, mounted, canonical)
    if http_config and not http_config_mounted:
        args += _bind(http_config[0], http_config[2], http_mode)

    args += harness.settings(provider, profile, env, name, canonical, http_config,
                             settings_mounted)
    args += environ.credentials(provider, env, remote)

    # MCP servers that exist only on this machine. Half of them cannot come
    # along at all — some are native macOS binaries — so the server
    # stays on the host and only its stdio is carried across. The shim is
    # mounted AT THE COMMAND'S OWN PATH, which means the harness's own config
    # needs no rewriting: it already points there. Servers reached over http are
    # left alone; the box can dial them itself.
    if profile.get("mcp") is False:
        return args
    # Several servers can share one command and differ only in its arguments —
    # every remote MCP server reached through mcp-remote does. A shim is
    # mounted at the command's path, so there is one shim for all of them, and
    # it tells them apart by the arguments it is handed.
    by_command = {}
    for server_name, server in sorted(mcp.mcp_servers(provider, env).items()):
        by_command.setdefault(server["command"][0], []).append((server_name, server))

    for target, group in sorted(by_command.items()):
        if target in claimed:
            # The same server declared by another harness in this box is the
            # same server: its shim is already there.
            continue
        routes = {}
        for server_name, server in group:
            live = mcp._start_bridge(server_name, server, profile, projects)
            if not live:
                continue
            for variable in server.get("inherit") or []:
                if os.environ.get(variable) and variable not in inherited:
                    inherited[variable] = os.environ[variable]
            routes[server_name] = (tuple(server["command"][1:]), live)
        if not routes:
            continue
        claimed[target] = ", ".join(sorted(routes))
        if len(routes) == 1:
            only = list(routes)[0]
            written = shim._write_shim(provider, only, routes[only][1])
        else:
            written = shim._write_shim_many(provider, target, routes)
        args += _bind(written, target, "ro")
    return args


def _parents(cmd, home):
    """Claim each directory a mount point's own parents would create as root.

    Docker makes a missing mount point and every parent above it itself, owned
    by root. Under a box's home that is a directory the box then cannot write
    to, and the failure names the child rather than the cause: "EACCES:
    permission denied, mkdir".

    Measured on finik, where six boxes of twelve had ~/.cache owned by
    root:root — the codex session database is mounted at
    ~/.cache/broker-box/codex-db/<id>, so the container made ~/.cache on the
    way to it, and a gate that wanted ~/.cache/finik/pre-push-gates could not
    have it.

    This was being cured one path at a time: ~/.bun and ~/.bun/install above,
    ~/.config above that, and a provider's own BOX_WRITABLE list. Every new
    mount deep under home is then the same bug again, found by whoever it
    breaks. The parents are derivable from the mounts themselves, so derive
    them.

    Only under home, and only where nothing is mounted already: a directory
    inside a bind-mounted project is the HOST's, and a tmpfs over it would hide
    what is really there. Everything else below home sits on the home tmpfs and
    is empty regardless, so claiming it hides nothing.
    """
    binds, taken = set(), set()
    for flag, value in zip(cmd, cmd[1:]):
        target = _target(flag, value)
        if not target:
            continue
        taken.add(target)
        if flag == "--mount":
            binds.add(target)

    want = set()
    for target in taken:
        parent = os.path.dirname(target)
        while parent.startswith(home + os.sep):
            want.add(parent)
            parent = os.path.dirname(parent)

    args = []
    for directory in sorted(want - taken):
        if any(directory.startswith(bind + os.sep) for bind in binds):
            continue
        args += ["--tmpfs", "%s:uid=%d,gid=%d,mode=0700,exec"
                 % (directory, os.getuid(), os.getgid())]
    return args


def _target(flag, value):
    if flag == "--tmpfs":
        return value.split(":", 1)[0]
    if flag == "--mount":
        for part in value.split(","):
            if part.startswith("target="):
                return part[len("target="):]
    return None


def _unclaimed(cmd, args):
    """`args` without the mounts `cmd` already has at the same place."""
    taken = {_target(flag, value) for flag, value in zip(cmd, cmd[1:])} - {None}
    kept = []
    for flag, value in zip(args[::2], args[1::2]):
        target = _target(flag, value)
        if target in taken:
            continue
        if target:
            taken.add(target)
        kept += [flag, value]
    return kept
