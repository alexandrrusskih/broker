"""Commands that read or tidy state: list, refresh, delete-auth, version."""

import os
import shutil
import subprocess
import sys

from . import accounts, api, config, profile, table
from .out import die, warn

try:
    from .version import STAMP
except ImportError:  # not stamped (running from a checkout)
    STAMP = {}


def cmd_list(cfg, provider, _args):
    names = api.list_accounts(cfg, provider.NAME, die)
    if not names:
        die("no %s accounts seeded — run '%s auth <name>'" % (provider.NAME, provider.CMD))
    table.render(cfg, provider, accounts.probe_all(cfg, provider, names))
    return 0


def cmd_startup(cfg, provider, args):
    """Show, set or clear the line prepended to every run of this harness.

    One line, not a list: everything that looks like KEY=VALUE at the front goes
    into the environment, the rest becomes arguments placed before yours. That
    covers the whole point of it — pinning a model, turning on a flag — while
    staying something you can read and rewrite in one command.

      broker-cl startup
      broker-cl startup 'ANTHROPIC_MODEL=claude-opus-5[1m] --verbose'
      broker-cl startup --clear
    """
    current = (cfg.get("startup") or {}).get(provider.NAME) or ""
    if not args:
        print(current or "(none)")
        return 0
    if args[0] in ("--clear", "-c", "none", ""):
        startup = dict(cfg.get("startup") or {})
        startup.pop(provider.NAME, None)
        config.save({"startup": startup})
        warn("startup cleared for %s" % provider.NAME)
        return 0

    line = " ".join(args).strip()
    env, argv = parse_startup(line)
    startup = dict(cfg.get("startup") or {})
    startup[provider.NAME] = line
    config.save({"startup": startup})
    warn("startup for %s: %s" % (provider.NAME, line))
    if env:
        warn("  environment: " + ", ".join("%s=%s" % kv for kv in env.items()))
    if argv:
        warn("  arguments:   " + " ".join(argv))
    return 0


def parse_startup(line):
    """Split a startup line into (environment, arguments).

    Leading NAME=VALUE words are environment; everything from the first
    non-assignment onwards is arguments, so a value may itself contain '='.
    """
    env, argv, rest = {}, [], False
    for word in (line or "").split():
        if not rest and "=" in word and word.split("=", 1)[0].isidentifier():
            key, value = word.split("=", 1)
            env[key] = value
        else:
            rest = True
            argv.append(word)
    return env, argv


def adopt_shared(provider, home):
    """Replace a profile's own copy of a now-shared file with a link to the
    canonical one, keeping the old copy beside it.

    Profiles created before a name became shared hold a real file, and
    link_shared leaves those alone on purpose — it must never overwrite
    something the user put there. This is the explicit opt-in.
    """
    moved = []
    for src, dst in profile.shared_entries(provider, home):
        name = os.path.relpath(dst, home)
        if not os.path.exists(src) or os.path.islink(dst) or not os.path.exists(dst):
            continue
        keep = dst + ".pre-share"
        try:
            os.replace(dst, keep)
            # The old database's sidecars would sit next to a symlink now, where
            # sqlite never looks (it creates them beside the real file) — leave
            # them beside their own database instead of confusing the next reader.
            for side in ("-wal", "-shm"):
                if os.path.exists(dst + side):
                    os.replace(dst + side, keep + side)
            os.symlink(src, dst)
            moved.append(name)
        except OSError as exc:
            warn("%s: could not adopt %s: %s" % (os.path.basename(home), name, exc))
    return moved


def cmd_refresh(cfg, provider, args):
    """Bring local profiles in line with what the broker holds.

    A bare run already creates and links the profiles it sees; this also installs
    each account's auth file, so every profile works straight away — including
    under a bare harness with an explicit config dir.
    """
    names = api.list_accounts(cfg, provider.NAME, die)
    if not names:
        die("no %s accounts seeded — run '%s auth <name>'" % (provider.NAME, provider.CMD))

    share = "--share" in args
    rows = accounts.probe_all(cfg, provider, names)
    for row in rows:
        home = profile.profile_dir(provider, row["account"])
        existed = os.path.isdir(home)
        try:
            if row["auth"]:
                profile.write_auth(provider, home, row["auth"])
            else:
                os.makedirs(home, mode=0o700, exist_ok=True)
                profile.prepare(provider, home)
        except OSError as exc:
            warn("%s: cannot prepare %s: %s" % (row["account"], home, exc))
            continue
        note = "" if existed else "(created)"
        if share:
            moved = adopt_shared(provider, home)
            if moved:
                note = "(now shares %s; old copies kept as *.pre-share)" % ", ".join(moved)
        warn("%-10s %s %s" % (row["account"], home, note))

    # Profiles left behind by an account that is no longer seeded. Never removed
    # automatically — they still hold that account's history.
    known = {profile.profile_dir(provider, n) for n in names}
    # Where profiles actually live. For codex that is beside ~/.codex; for agy the
    # canonical home is $HOME itself, so deriving it from there walked /Users and
    # reported strangers' home directories as orphan profiles.
    parent, prefix = profile.profile_root(provider)
    for entry in sorted(os.listdir(parent)):
        path = os.path.join(parent, entry)
        if not (entry.startswith(prefix) and os.path.isdir(path)) or path in known:
            continue
        # The account is gone from the broker but its credentials are still here.
        # For agy that is a working refresh token, so leaving it is leaving the
        # account usable by anyone with this disk. The history is what the profile
        # is kept for, not the token.
        stale = os.path.join(path, provider.AUTH_NAME)
        if os.path.isfile(stale):
            try:
                os.remove(stale)
                warn("orphan profile %s — removed its stale credentials" % path)
                continue
            except OSError as exc:
                warn("orphan profile %s — could not remove its credentials: %s" % (path, exc))
                continue
        warn("orphan profile %s — no such account in the broker" % path)

    if not share:
        warn("")
        warn("profiles created earlier keep their own history/state files;")
        warn("run '%s refresh --share' to point them at the shared ones." % provider.CMD)

    print("", file=sys.stderr)
    table.render(cfg, provider, rows)
    return 0


def cmd_delete_auth(cfg, provider, args):
    """Forget an account in the broker.

    The refresh token goes with it, so this is irreversible: getting the account
    back means a fresh login plus a new seed. The local profile is left alone —
    it holds that account's history.
    """
    names = [a for a in args if not a.startswith("-")]
    if not names:
        die("usage: %s delete-auth <name> [--yes]" % provider.CMD)
    account = names[0]

    known = api.list_accounts(cfg, provider.NAME, die)
    if account not in known:
        die("no such account: %s (have: %s)" % (account, ", ".join(known) or "none"))

    if "--yes" not in args and "-y" not in args:
        if not sys.stdin.isatty():
            die("refusing to delete without a terminal — pass --yes to mean it")
        try:
            answer = input(
                "delete '%s' from the broker? its refresh token is gone for good. "
                "type the name to confirm: " % account
            ).strip()
        except EOFError:
            answer = ""
        if answer != account:
            die("not confirmed — nothing deleted")

    if not api.delete_account(cfg, provider.NAME, account, die):
        warn("%s was not in the broker" % account)
    warn("%s deleted from the broker" % account)

    dropped = drop_credentials(provider, account)
    for path in dropped:
        warn("removed its credentials: %s" % path)

    home = profile.profile_dir(provider, account)
    if getattr(provider, "CREDENTIALS", "file") != "env" and os.path.isdir(home):
        warn("its profile is kept: %s   (it holds this account's history)" % home)
    return 0


def drop_credentials(provider, account):
    """Delete this account's credentials from the machine, keeping its history.

    Revoking an account in the broker used to leave its token sitting in the
    profile — and for agy that is the REAL refresh token, so "deleted" meant
    deleted in one place only. History stays: it is the reason the profile
    survives at all.
    """
    removed = []
    candidates = [profile.cache_path(provider, account)]
    if getattr(provider, "CREDENTIALS", "file") != "env":
        candidates.append(os.path.join(profile.profile_dir(provider, account), provider.AUTH_NAME))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError as exc:
            warn("could not remove %s: %s" % (path, exc))
    return removed


def cmd_version(cfg, provider, _args):
    """What is installed, and whether this wrapper is behind the repo."""
    stamp = STAMP.get("commit") or "unstamped"
    if STAMP.get("date"):
        stamp += "  (%s)" % STAMP["date"]
    print("%-8s %s" % (provider.CMD, stamp))
    if STAMP.get("pkg"):
        print("%-8s %s   %s" % ("broker", STAMP["pkg"], shutil.which("broker") or "not in PATH"))

    from .run import real_bin

    target = real_bin(provider)
    if not target:
        print("%-8s not found" % provider.BIN)
    else:
        try:
            out = subprocess.run(
                [target, "--version"], capture_output=True, text=True, timeout=30
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            out = "could not run: %s" % exc
        print("%-8s %s" % (provider.BIN, out))

    # Is this wrapper the newest one? Ask the repo directly — cheap, no clone.
    from .install import SRC_REPO

    commit = STAMP.get("commit")
    if not (shutil.which("git") and commit):
        return 0
    repo = cfg.get("src_repo") or SRC_REPO
    try:
        out = subprocess.run(
            ["git", "ls-remote", repo, "HEAD"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn("could not reach %s: %s" % (repo, exc))
        return 0
    head = (out.stdout.split() or [""])[0]
    if not head:
        warn("could not read HEAD from %s" % repo)
        return 0
    # strip the +dirty marker: the comparison is about the commit, not the tree
    if head.startswith(commit.split("+")[0]):
        print("\nup to date with %s" % repo)
    else:
        print("\nupdate available — %s is at %s; run '%s upgrade'" % (repo, head[:7], provider.CMD))
    return 0
