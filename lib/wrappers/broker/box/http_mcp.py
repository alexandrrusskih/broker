"""Give a box its own MCP config with host HTTP endpoints it can reach."""

import hashlib
import json
import os
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .. import config
from ..out import warn
from .boxes import _strip_comments
from .paths import expand, window_key


def _config_path(provider, env):
    spec = getattr(provider, "MCP_CONFIG", None)
    if not spec:
        return None
    path = os.path.abspath(expand(spec[0]))
    home_env = getattr(provider, "HOME_ENV", None)
    canonical = getattr(provider, "CANONICAL_HOME", None)
    in_use = (env or {}).get(home_env) if home_env else None
    if in_use and canonical:
        canonical = os.path.abspath(expand(canonical))
        if path.startswith(canonical + os.sep):
            path = os.path.join(os.path.abspath(expand(in_use)),
                                os.path.relpath(path, canonical))
    return path


def _box_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return None
    if parsed.username or parsed.password:
        return None
    host = "host.docker.internal"
    if port is not None:
        host += ":%d" % port
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def codex_args(provider, env, profile=None):
    """Spell out, on the command line, what codex's config file cannot say here.

    Two things. A loopback MCP URL, which the box cannot dial as it stands. And
    a server the BOX declares (see mcp.declared): codex's config travels
    read-only, so the declaration arrives as `-c` instead of as a line in the
    file.
    """
    if provider.NAME != "codex":
        return []
    extra = []
    from .mcp import declared

    for name, server in sorted(declared(profile or {}).items()):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            warn("cannot declare MCP server %r for the box: the name is not a "
                 "bare TOML key" % name)
            continue
        argv = server["command"]
        extra += ["-c", "mcp_servers.%s.command=%s" % (name, json.dumps(argv[0]))]
        if argv[1:]:
            extra += ["-c", "mcp_servers.%s.args=%s" % (name, json.dumps(list(argv[1:])))]
        for key, value in sorted((server.get("env") or {}).items()):
            extra += ["-c", "mcp_servers.%s.env.%s=%s" % (name, key, json.dumps(str(value)))]
    path = _config_path(provider, env)
    if not path or not os.path.isfile(path):
        return extra
    try:
        import tomllib

        with open(path, "rb") as handle:
            servers = tomllib.load(handle).get("mcp_servers", {})
    except (OSError, ValueError) as error:
        warn("could not read MCP URLs for the box: %s" % error)
        return extra
    args = list(extra)
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        url = _box_url(server.get("url"))
        if url and not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            warn("cannot override MCP URL for server %r in the box" % name)
            continue
        if url:
            args.extend(["-c", "mcp_servers.%s.url=%s" % (name, json.dumps(url))])
    return args


def _shape(provider, server):
    """One declared server, as THIS harness spells a server it starts itself."""
    argv = server["command"]
    if provider.NAME == "opencode":
        entry = {"type": "local", "command": list(argv), "enabled": True}
        if server.get("env"):
            entry["environment"] = dict(server["env"])
        return entry
    entry = {"command": argv[0], "args": list(argv[1:])}
    if provider.NAME == "claude":
        entry["type"] = "stdio"
    else:
        entry["disabled"] = False
    if server.get("env"):
        entry["env"] = dict(server["env"])
    return entry


def stage(provider, env, box, profile=None):
    """Return (box copy, source path, mount target), or None if nothing changes.

    Two things can change: a loopback URL, which the box cannot dial as it
    stands, and a server the BOX declares (see mcp.declared) and the machine's
    own config says nothing about.
    """
    path = _config_path(provider, env)
    if not path or not os.path.isfile(path):
        return None
    kind, key = provider.MCP_CONFIG[1:]
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        original = text
        if kind == "toml":
            # Imported here and not at the top, as box/mcp.py does it: tomllib
            # arrived in 3.11, and this module is reached from box/run.py, which
            # every wrapper imports. At the top, one import made the WHOLE broker
            # need 3.11 — on a PATH where python3 is the system 3.9, all four
            # wrappers died with "cannot find the broker engine (No module named
            # 'tomllib')", including paths that read no TOML at all.
            import tomllib

            data = tomllib.loads(text)
        elif kind == "jsonc":
            data = json.loads(_strip_comments(text))
        else:
            data = json.loads(text)
    except (OSError, ValueError) as error:
        warn("could not read MCP URLs for the box: %s" % error)
        return None
    servers = data.get(key) or {}
    if not isinstance(servers, dict):
        return None
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        for name in ("url", "serverUrl"):
            old = server.get(name)
            new = _box_url(old)
            if new:
                text = text.replace(old, new)

    # What the box declares for itself. Written out as JSON rather than patched
    # into the text: there is no line to replace, the server is not in the file
    # at all. The box's copy loses the comments of the original, which only the
    # person editing the real file reads.
    from .mcp import declared

    extra = declared(profile or {}) if kind in ("json", "jsonc") else {}
    if extra:
        if kind == "jsonc":
            data = json.loads(_strip_comments(text))
        else:
            data = json.loads(text)
        servers = data.setdefault(key, {})
        for name, server in sorted(extra.items()):
            servers[name] = _shape(provider, server)
        text = json.dumps(data, indent=2)
    if text == original:
        return None
    directory = os.path.join(config.CONFIG_DIR, "box", "http-mcp", box, window_key())
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # The name says what is in it, so a config that changed is a new file
        # and the one a running box has mounted is never replaced under it. The
        # same rule as shim._put, and for the second reason given there too:
        # Docker Desktop keeps its own cache of the host filesystem, and a bind
        # source it once failed to find stays "does not exist" for minutes.
        target = os.path.join(directory, "%s-%s-%s" % (
            provider.NAME, os.path.basename(path),
            hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]))
        if os.path.exists(target):
            return target, path, os.path.realpath(path)
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError as error:
        warn("could not stage HTTP MCP config for the box: %s" % error)
        return None
    return target, path, os.path.realpath(path)
