"""Give a box its own MCP config with host HTTP endpoints it can reach."""

import json
import os
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


def stage(provider, env, box):
    """Return (box copy, source path, mount target), or None if no URL changes."""
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
        for key in ("url", "serverUrl"):
            old = server.get(key)
            new = _box_url(old)
            if new:
                text = text.replace(old, new)
    if text == original:
        return None
    directory = os.path.join(config.CONFIG_DIR, "box", "http-mcp", box, window_key())
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        target = os.path.join(directory, provider.NAME + "-" + os.path.basename(path))
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
