"""MCP servers a box would otherwise lose.

Half of them cannot come along: they are native macOS binaries, and a Linux
container has nothing to run them with. So the server stays on the host and only
its stdio is carried across — a listener here, a stand-in mounted at the
server's own command path there.
"""

import json
import os
import subprocess
import sys
import time

from .. import config
from ..out import warn
from .paths import expand

# Inside a container "localhost" is the container. This is the host.
HOST_GATEWAY = "host.docker.internal"

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
        elif kind == "jsonc":
            # JSON with comments. Written by hand, so it has them.
            from .boxes import _strip_comments

            with open(path) as fh:
                data = json.loads(_strip_comments(fh.read()))
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
        # Some spell the command as a string plus arguments, others as one
        # list. Both mean the same thing.
        if isinstance(command, (list, tuple)):
            argv = [str(part) for part in command]
        else:
            argv = [command] + list(server.get("args") or [])
        found[name] = {
            "command": argv,
            # "environment" is what opencode calls it; "env" everywhere else.
            "env": dict(server.get("env") or server.get("environment") or {}),
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
    env.update(profile.get("env") or {})
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


def _declared(name, server, profile):
    """The names this box or this harness SET for the server, not what it inherits.

    os.environ is most of a listener's environment and none of its declaration,
    so only what somebody wrote down is checked below.
    """
    names = list(server.get("env") or {})
    names += [v for v in (profile.get("env") or {}) if v not in names]
    names += [v for v in (((profile.get("mcp") or {}).get(name) or {}).get("env") or {})
              if v not in names]
    return names


def _complain(name, server, profile, bridge_env):
    """Say out loud what will make this server fail, instead of letting it die quiet.

    A server that cannot start leaves the harness showing one red entry and no
    reason. Measured: agy pointed codebase-memory at a snapshot directory that
    did not exist, the server exited with 'daemon session context was rejected'
    — which names a daemon, not a path — and it stayed broken for a month
    because nothing on the way said anything.

    A warning only. The server may well create the directory it was given, and
    a box that starts without one message is worth more than a box that refuses
    to start because we guessed wrong.
    """
    binary = (server.get("command") or [""])[0]
    if binary and os.sep in binary and not os.path.exists(binary):
        warn("%s: its command %s does not exist — the server cannot start" % (name, binary))
    for variable in _declared(name, server, profile):
        value = bridge_env.get(variable)
        if value and value.startswith(os.sep) and not os.path.exists(value):
            warn("%s: %s=%s does not exist — the server may refuse to start"
                 % (name, variable, value))


def _start_bridge(name, server, profile, projects):
    """Make sure a listener for this server is up, and say how to reach it."""
    from .. import mcpbridge
    from .. import mcpserve

    # Same server, same caller identity — anything else gets its own listener.
    # Which listener this is. Same server and same caller is the same listener
    # — but ALSO the same environment: a server started once keeps whatever it
    # was started with, and a variable the person sets on the command line to
    # change how it behaves would be ignored by a listener raised before they
    # typed it. That is not a small thing when the variable decides WHO the
    # server acts as: the work is done under the wrong name, and the store
    # refuses it as read-only, which reads as a permissions problem rather than
    # as a stale process.
    #
    # So the values of the variables this server actually receives from the
    # environment are part of its identity. Change one, get your own listener.
    bridge_env = _bridge_env(name, server, profile, projects)
    matters = list(((profile.get("mcp") or {}).get(name) or {}).get("identity_env") or ())
    matters += [v for v in (profile.get("env") or {}) if v not in matters]
    matters += [v for v in (server.get("inherit") or []) if v not in matters]
    matters += [v for v in (server.get("env") or {}) if v not in matters]
    matters += [v for v in (((profile.get("mcp") or {}).get(name) or {}).get("env") or {})
                if v not in matters]

    # What this box may ASK of the server, as opposed to what the server is
    # started with. codebase-memory is the case: its allowed root governs
    # indexing, not reading, so a box with a root of its own could still read
    # every project in the shared base. The rule travels in the environment
    # because the listener already inherits it, and its name joins `matters` —
    # two boxes with different rules must never share a listener, or the
    # second one would be answered under the first one's rule.
    rule = ((profile.get("mcp") or {}).get(name) or {}).get("policy")
    if rule:
        bridge_env[mcpserve.POLICY_ENV] = json.dumps(rule, sort_keys=True)
        matters.append(mcpserve.POLICY_ENV)

    _complain(name, server, profile, bridge_env)

    key = mcpbridge.identity_key(env=bridge_env, extra=tuple(matters))
    live = mcpbridge.running(name, key)
    if live and live.get("command") == server["command"]:
        return live
    # Where the package is imported FROM — this file sits in broker/box/, so
    # three levels up. It was two before the engine was split into modules,
    # and the bridge silently stopped starting: "the bridge did not come up",
    # with every MCP server missing inside the box.
    engine = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # It used to be the cwd, which made it the SERVER's working directory too,
    # because the server inherits it. codebase-memory then took the engine
    # directory for its workspace, could not watch a path outside its allowed
    # root, and logged daemon.workspace.skipped on every request — so file
    # watching was off in every bridged session, silently, and the symptom was
    # an index that never noticed an edit. The import keeps working through
    # PYTHONPATH, and the directory a server starts in is the one it is meant
    # to be looking at.
    bridge_env = dict(bridge_env)
    bridge_env["PYTHONPATH"] = os.pathsep.join(
        [engine] + ([bridge_env["PYTHONPATH"]] if bridge_env.get("PYTHONPATH") else []))
    workspace = os.path.realpath(projects[0]) if projects else engine
    if not os.path.isdir(workspace):
        workspace = engine
    try:
        subprocess.Popen(
            [sys.executable, "-m", "broker.mcpbridge", "serve", name, "--key", key, "--"]
            + server["command"],
            cwd=workspace,
            env=bridge_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # it outlives this process: the box is its client
        )
    except OSError as exc:
        warn("could not bridge the %s MCP server: %s" % (name, exc))
        return None
    for _ in range(50):
        # Not verified: we are waiting for the listener this call just
        # started, so asking the system about its pid fifty times over would
        # only spend processes on an answer we already have.
        live = mcpbridge.running(name, key, verify=False)
        if live:
            return live
        time.sleep(0.1)
    warn("the %s MCP bridge did not come up — it will be missing inside the box" % name)
    return None
