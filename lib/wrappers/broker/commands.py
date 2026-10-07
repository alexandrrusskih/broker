"""Commands that read or tidy state: list, refresh, delete-auth, version."""

import shutil
import subprocess

from . import accounts, api, config, table
from .out import die, warn

try:
    from .version import STAMP
except ImportError:  # not stamped (running from a checkout)
    STAMP = {}


def cmd_list(cfg, provider, _args):
    names = api.list_accounts(cfg, provider.NAME, die)
    if not names:
        die("no %s accounts seeded — run '%s auth <name>'" % (provider.NAME, provider.CMD))
    # Always measured, never remembered: this command exists to answer "what is
    # left right now", and a cached number is not an answer to that.
    table.render(cfg, provider, accounts.probe_all(cfg, provider, names, fresh=True))
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
    from .upgrade import SRC_REPO

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
