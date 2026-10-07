"""Per-account profiles: their own auth file, shared everything else."""

import os
from pathlib import Path

from .tree import _entries


def profile_dir(provider, account):
    """Where this account's config dir lives. An explicit one always wins.

    Every account gets its own directory, including whichever one you started
    with: the canonical home stays put as the shared original the profiles link
    back to, so no account inherits it by being special.
    """
    env_name = getattr(provider, "PROFILE_ENV", provider.HOME_ENV)
    explicit = env_name and os.environ.get(env_name)
    if explicit:
        return os.path.expanduser(explicit)
    base = getattr(provider, "PROFILE_BASE", None) or provider.CANONICAL_HOME
    return "%s-%s" % (base, account)


def shared_names(provider):
    """Everything shared with the canonical home: fixed names plus glob matches.

    A mirroring provider has no such list — everything except the credentials is
    shared — so this is empty for one, and callers use shared_entries instead.
    """
    private = getattr(provider, "PRIVATE", None)
    if private is not None:
        # Everything is shared except what genuinely belongs to ONE account.
        # Listing what to share instead of what to keep apart is a list that is
        # always one item out of date: the harness invents a file, nobody adds
        # it here, and the second account silently gets its own copy — which is
        # how a login granted hours earlier came back as "authentication
        # required". What is private is short, known, and does not grow.
        skip = set(private) | {"." , ".."}
        names = []
        for entry in sorted(os.listdir(provider.CANONICAL_HOME)):
            if entry in skip or entry.startswith(".tmp"):
                continue
            # Copies kept aside by hand while fixing something. Linking them
            # would spread yesterday's rescue across every profile.
            if any(mark in entry for mark in (".bak-", ".pre-share", ".revoked-", ".old-")):
                continue
            # Sidecars belong to the file they sit beside: sqlite makes them
            # itself, next to whichever copy of the database is in use.
            if entry.endswith(("-wal", "-shm")):
                continue
            names.append(entry)
        return names

    names = list(getattr(provider, "SHARED", ()))
    for pattern in getattr(provider, "SHARED_GLOBS", ()):
        names += [p.name for p in Path(provider.CANONICAL_HOME).glob(pattern)]
    return names


# A run that must start with none of your MCP servers, while keeping the login.
#
# Only the mirrored layout needs this. codex hands its profile a variable of its
# own and already owns its config.toml; claude has no profile at all, by design
# (see run.py); and for opencode the broker is not on the host path — cli.py
# sends a provider with no credentials straight to exec_passthrough, so there is
# nothing here to ask. agy is the one left: its profile mirrors your home, and
# the mirror shares .gemini/config whole, so the MCP file in a profile IS yours.


ISOLATE_MCP_ENV = "BROKER_ISOLATE_MCP"


def _mcp_relative(provider):
    """Where this provider's MCP file sits inside the canonical home, or None."""
    declared = getattr(provider, "MCP_CONFIG", None)
    if not declared:
        return None
    absolute = os.path.realpath(os.path.expanduser(declared[0]))
    canonical = os.path.realpath(provider.CANONICAL_HOME)
    relative = os.path.relpath(absolute, canonical)
    # Outside the home it is not the mirror's to split.
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        return None
    return relative


def private_paths(provider, isolate_mcp=None):
    """Which paths this profile owns rather than shares, deepest-first order.

    The credentials always. The MCP file only when this run asked for it — the
    default is every byte of the previous behaviour.
    """
    paths = [provider.AUTH_NAME]
    if isolate_mcp is None:
        isolate_mcp = bool(os.environ.get(ISOLATE_MCP_ENV))
    if isolate_mcp:
        relative = _mcp_relative(provider)
        if relative:
            paths.append(relative)
    return paths


def _private_tree(paths):
    """The private paths as a prefix tree. A leaf — an empty dict — is the file.

    A tree and not a single list of components because there can now be two of
    them, and they diverge: .gemini/antigravity-cli/<token> and
    .gemini/config/mcp_config.json share one level and then part. Walking each
    separately would share .gemini/config while splitting it.
    """
    tree = {}
    for path in paths:
        node = tree
        for part in path.split(os.sep):
            if not part or part == os.curdir:
                continue
            node = node.setdefault(part, {})
    return tree


def _walk_mirror(canonical, dst, tree, visit):
    """Call visit(src, dst) for every shared entry, descending the private ones.

    With one private path this produces exactly the sequence the single-path
    loop produced before it, which is what keeps the default untouched.
    """
    for name in _entries(canonical):
        if name in tree:
            continue
        visit(os.path.join(canonical, name), os.path.join(dst, name))
    for name, below in tree.items():
        if not below:
            continue  # the private file itself: nothing under it to share
        _walk_mirror(os.path.join(canonical, name),
                     os.path.join(dst, name), below, visit)


def shared_entries(provider, path, isolate_mcp=None):
    """(canonical source, place in this profile) for everything meant to be shared.

    The two profile layouts answer this differently — a list of names for codex,
    every entry except the path down to the token for agy — and callers that only
    want "what should be a link here" should not have to know which is which.
    """
    if not getattr(provider, "MIRROR_HOME", False):
        return [
            (os.path.join(provider.CANONICAL_HOME, name), os.path.join(path, name))
            for name in shared_names(provider)
        ]

    pairs = []
    _walk_mirror(provider.CANONICAL_HOME, path,
                 _private_tree(private_paths(provider, isolate_mcp)),
                 lambda src, dst: pairs.append((src, dst)))
    return pairs


def profile_root(provider):
    """The directory profiles live beside, and the prefix their names carry."""
    base = getattr(provider, "PROFILE_BASE", None) or provider.CANONICAL_HOME
    return os.path.dirname(base), os.path.basename(base) + "-"
