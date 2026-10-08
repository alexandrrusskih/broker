"""The harness's own files and directories, as the box sees them."""

import os
import re
import shutil
import uuid

from .. import config
from ..out import warn
from . import http_mcp
from .paths import _bind, _empty_file, _mount, expand


def own_home(provider, profile, env, name, mounted):
    """Mount the harness home. Appends to `mounted` what it mounted whole."""
    args = []
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
            args += _bind(http_config[0], host, http_mode)
            http_config_mounted = True
            settings_mounted.add(host)
            continue
        if os.path.isdir(host):
            args += _mount(host)
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
        args += _bind(copy, host, mode)
        settings_mounted.add(host)
    # ...minus its credentials file. The token comes from the broker below.
    blank = None
    for entry in getattr(provider, "BOX_SECRETS", ()):
        host = expand(entry)
        if any(host.startswith(m + os.sep) for m in mounted):
            blank = blank or _empty_file()
            args += _bind(blank, host, "ro")
    return (args, http_config, http_config_mounted, http_mode, settings_mounted)


def siblings(provider, canonical):
    """Every profile of this harness, not only the one this run uses."""
    args = []
    # Directories that every profile of this harness should reach, not only the
    # one this run uses: a harness can record a path through a profile it is no
    # longer using, and the file it names is shared anyway.
    shared = getattr(provider, "BOX_SHARED", ())
    if shared:
        import glob as globmodule

        for sibling in sorted(globmodule.glob(canonical + "-*")):
            for entry in shared:
                host = os.path.join(sibling, entry)
                if os.path.exists(host):
                    args += _bind(os.path.realpath(host), host)
    return args


def chosen_home(provider, env, name, remote, mounted, canonical):
    """The config directory this run was pointed at, and the name for it."""
    args = []
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
            args += _mount(source, target=host)
            args += ["-e", "%s=%s" % (home_env, host)]
    return args


# An absolute path named inside a settings file: quoted — so it may hold spaces
# — or bare. Every candidate is checked against the filesystem below, so being
# generous here costs nothing and missing a form costs the protection.
SCRIPTS = (re.compile(r"'(/[^']+)'"),
           re.compile(r'"(/[^"]+)"'),
           re.compile(r"(/[\w./+-]+)"))


def _named_scripts(sources, protected):
    """Every script the read-only settings tell the harness to run, read-only too.

    The settings files are read-only above, and that is not enough: a command
    inside one NAMES a script, and those sit in the harness's own directory —
    which a box gets writable, because that is where the harness keeps its
    state. So a box could rewrite one and the HOST harness would run it at its
    next session event, outside any container. Confirmed on this machine for
    ~/.claude/hooks/herdr-agent-state.sh and ~/.codex/herdr-agent-state.sh.

    `sources` is every settings file mounted read-only just above, including the
    per-account profile copies — not the provider's declared list, which leaves
    those out.

    The boundary is the directory each settings file lives in, and not the
    harness home: agy's home IS your home, so "under the home" would have meant
    every tool you own — including the MCP servers mounted by name further down,
    which docker then refuses as a duplicate mount point. A settings file that
    sits directly in the home is skipped for the same reason.
    """
    args = []
    seen = set(protected)
    # Compared as written, not resolved: a settings file names a path the way
    # the person typed it, and resolving one side only never matches.
    home = expand("~") + os.sep
    for host in sources:
        inside = os.path.dirname(host) + os.sep
        if inside == home or home.startswith(inside):
            continue
        try:
            with open(host, encoding="utf-8", errors="replace") as handle:
                # JSON holds a quoted path as \", and the quote is what delimits
                # a name with a space in it.
                text = handle.read().replace('\\"', '"')
        except OSError:
            continue
        for pattern in SCRIPTS:
            for found in pattern.findall(text):
                if found in seen or not found.startswith(inside):
                    continue
                if not os.path.isfile(found):
                    continue
                seen.add(found)
                args += _mount(os.path.realpath(found), "ro", found)
    return args


def settings(provider, env, canonical, http_config, settings_mounted):
    """What the host decided and the box may read but not rewrite."""
    args = []
    # Host settings stay read-only. Runtime state in the same home stays writable.
    config_target = http_config[1] if http_config else None
    protected = set(settings_mounted)
    sources = []
    for entry in getattr(provider, "BOX_SETTINGS", ()):
        host = expand(entry)
        if os.path.isfile(host):
            sources.append(host)
        if os.path.isfile(host) and host != config_target and host not in settings_mounted:
            args += _mount(os.path.realpath(host), "ro", host)
            protected.add(host)
    if provider.NAME == "codex" and env.get("CODEX_HOME"):
        profile_home = os.path.abspath(expand(env["CODEX_HOME"]))
        if profile_home != canonical:
            for filename in ("config.toml", "hooks.json"):
                host = os.path.join(profile_home, filename)
                if os.path.isfile(host):
                    args += _mount(os.path.realpath(host), "ro", host)
                    protected.add(host)
                    sources.append(host)
    if config_target:
        protected.add(config_target)
    args += _named_scripts(sources, protected)
    return args
