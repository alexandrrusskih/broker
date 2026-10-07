"""What the machine lends a box besides the harness: identity, keys, caches."""

import os
import re

from .. import config
from ..out import warn
from . import ssh
from .clones import _clone
from .paths import _bind, _mount, _passwd_file, expand


# What every box gets regardless of harness, read-only. Without a gitconfig the
# tools inside behave subtly differently from the same tools outside: git reads
# history fine, then refuses to commit for want of a user.email — and the
# harness discovers that halfway through a task.
COMMON_RO = ("~/.gitconfig",)


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


def tools(binary, image, name, profile, home):
    """Who the box thinks you are, and the few host files every harness wants."""
    args = []
    passwd = _passwd_file(binary, image)
    if passwd:
        args += _bind(passwd, "/etc/passwd", "ro")

    for entry in COMMON_RO:
        host = expand(entry)
        if os.path.exists(host):
            args += _mount(host, "ro")

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
            args += _bind(stub, expand(target), "ro")

    # Named keys only, at their own paths, so a tool that resolves ~/.ssh/<name>
    # finds what it expects and nothing else is there to find.
    ssh_config, ssh_keys, ssh_known = ssh._ssh_config(name, profile)
    if ssh_config:
        args += _bind(ssh_config, os.path.join(home, ".ssh", "config"), "ro")
        for key in ssh_keys:
            args += _bind(key, key, "ro")
        if ssh_known:
            args += _bind(ssh_known, os.path.join(home, ".ssh", "known_hosts"), "ro")
    return args


def databases(provider, env):
    """This window's own databases, at the same path inside as outside."""
    args = []
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
            args += _mount(own_databases, "rw")
    return args


def gitlab(profile, env, name, home):
    """The host line glab looks for, which a Keychain cannot hand a container."""
    args = []
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
            args += _bind(where, os.path.join(home, ".config", "glab-cli", "config.yml"), "ro")
    return args


def own_copies(provider, profile, name):
    """What the box named as its own: cloned in, and what it writes stays inside."""
    args = []
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
        args += _bind(copy, host)
    return args
