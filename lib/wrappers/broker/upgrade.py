"""Updating the broker from source, and the harness behind a wrapper."""

import json
import os
import shutil
import subprocess
import sys

from . import config
from .out import die, warn


# `upgrade` installs the CLI straight from the source repo — the only source
# there is. A fix to the wrapper is one push away, with no publish step to
# forget. Override per machine with "src_repo"/"src_subdir" in the broker config,
# or install from a checkout with --from <dir>.
SRC_REPO = "git@github.com:alexandrrusskih/broker.git"
SRC_SUBDIR = ""
SRC_CACHE = config.SRC_CACHE


def run(cmd):
    warn(" ".join(cmd))
    return subprocess.call(cmd)


def _remote_of(path):
    """Which repository this checkout came from, or None if it cannot say."""
    try:
        out = subprocess.check_output(["git", "-C", path, "remote", "get-url", "origin"],
                                      stderr=subprocess.DEVNULL, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip()


def sync_source(cfg):
    """Clone or refresh the broker source; returns the package dir, or None.

    The checkout is ours alone (under ~/.cache), so a hard reset is safe — it
    never touches a working tree the user might have open elsewhere.
    """
    if not shutil.which("git"):
        warn("git not in PATH")
        return None
    repo = cfg.get("src_repo") or SRC_REPO
    # The cache remembers whichever repository it was first cloned from. When the
    # source moves — as it did when the broker moved out into its own
    # repository — fetching into the old checkout pulls the WRONG project, and the
    # upgrade then fails on a missing package.json while reporting the new address
    # as unreachable. Re-clone whenever the remote no longer matches.
    if os.path.isdir(os.path.join(SRC_CACHE, ".git")) and _remote_of(SRC_CACHE) != repo:
        warn("source moved to %s — re-cloning the cache" % repo)
        shutil.rmtree(SRC_CACHE, ignore_errors=True)
    if os.path.isdir(os.path.join(SRC_CACHE, ".git")):
        ok = run(["git", "-C", SRC_CACHE, "fetch", "--depth", "1", "origin", "HEAD"]) == 0
        ok = ok and run(["git", "-C", SRC_CACHE, "reset", "--hard", "FETCH_HEAD"]) == 0
    else:
        os.makedirs(os.path.dirname(SRC_CACHE), exist_ok=True)
        ok = run(["git", "clone", "--depth", "1", repo, SRC_CACHE]) == 0
    if not ok:
        return None

    subdir = cfg.get("src_subdir", SRC_SUBDIR)
    pkg = os.path.join(SRC_CACHE, subdir) if subdir else SRC_CACHE
    if not os.path.isfile(os.path.join(pkg, "package.json")):
        warn("no package.json in %s" % pkg)
        return None
    return pkg


def install_global(pkg):
    """Install the CLI from a local checkout.

    The old copy is removed first: installing the same path again appends a
    second entry to bun's global manifest instead of replacing it, and a few
    upgrades in, the lockfile no longer parses.
    """
    try:
        with open(os.path.join(pkg, "package.json")) as fh:
            name = json.load(fh).get("name")
    except (OSError, ValueError):
        name = None
    for tool in ("bun", "npm"):
        if not shutil.which(tool):
            continue
        if name:
            subprocess.call([tool, "remove", "-g", name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return run([tool, "install", "-g", pkg]) == 0
    warn("need bun or npm to install the broker CLI")
    return False


def cmd_upgrade(cfg, provider, args):
    """Update the chain: broker CLI (from git) -> this wrapper -> the harness.

    Re-wrapping is what pulls a new version of the package onto disk, so it runs
    after the CLI update and before the harness's own.
    """
    failed = 0
    local = None
    if "--from" in args:
        i = args.index("--from")
        if i + 1 >= len(args):
            die("usage: %s upgrade --from <dir>" % provider.CMD)
        local = os.path.expanduser(args[i + 1])

    if local:
        if not os.path.isfile(os.path.join(local, "package.json")):
            die("no package.json in %s" % local)
        if not install_global(local):
            die("could not install from %s" % local)
    else:
        pkg = sync_source(cfg)
        # There is no second source to fall back to, and that is the point: a
        # published package trails the repo and would silently downgrade.
        if not pkg:
            die("could not reach %s — check your access, or upgrade from a local "
                "checkout with '%s upgrade --from <dir>'"
                % (cfg.get("src_repo") or SRC_REPO, provider.CMD))
        if not install_global(pkg):
            die("could not install the broker CLI from %s" % pkg)

    # `broker <provider> install` lays down the wrapper AND the shim; the plain
    # `broker wrap` that used to run here did the first half, and then the call at
    # the end of this function did both again.
    if shutil.which("broker"):
        failed += 1 if run(["broker", provider.NAME, "install", "--no-ask"]) else 0
    else:
        warn("broker CLI not in PATH — %s was left as it is" % os.path.basename(sys.argv[0]))

    from .run import real_bin

    target = real_bin(provider)
    if target:
        failed += 1 if run([target, "update"]) else 0
    else:
        warn("cannot find the real %s — skipping its update" % provider.BIN)

    # The harness's own updater just rewrote the native name, taking the shim with
    # it. Put it back — a no-op when no shim was installed in the first place.
    if shutil.which("broker"):
        run(["broker", provider.NAME, "install", "--no-ask"])

    return 1 if failed else 0
