"""Argument handling. One engine, one entry point per harness.

One wrapper per harness — broker-cx, broker-cl, broker-agy — all of them this
engine with a different provider table:

  <cmd> [args...]              run the harness on the account picked for you
  <cmd> account | <cmd> list   show every account's remaining limit and exit
  <cmd> account <name> [args]  run on that account
  <cmd> auth <name>            log a new account in and hand it to the broker
  <cmd> delete-auth <name>     forget an account in the broker (asks first)
  <cmd> refresh                create and link a profile for every seeded account
  <cmd> version                versions, and whether this wrapper is current
  <cmd> upgrade [--from <dir>] update the broker CLI, this wrapper and the harness
"""

import importlib
import os
import sys

from . import api, config, out, run, select

COMMANDS = {
    "list": ("commands", "cmd_list"),
    "ls": ("commands", "cmd_list"),
    "refresh": ("commands", "cmd_refresh"),
    "delete-auth": ("commands", "cmd_delete_auth"),
    "version": ("commands", "cmd_version"),
    "startup": ("commands", "cmd_startup"),
    "auth": ("install", "cmd_auth"),
    "upgrade": ("install", "cmd_upgrade"),
}


def _still_holds_credentials(provider):
    """Whether a nested call really inherited what it needs to run.

    Skipping the broker for a nested call assumes the child already holds the
    credentials — true when they live in a file the profile points at, but a
    provider that takes them through the environment depends on the variable
    surviving. Agent sessions strip it from what they hand subprocesses, so the
    marker said "nested" while the token was gone, and the harness started with
    no credentials at all: "Not logged in · Please run /login".
    """
    if getattr(provider, "CREDENTIALS", "file") != "env":
        return True
    return bool(os.environ.get(provider.ENV_NAME))


def _account_from_env(provider):
    for name in ("%s_ACCOUNT" % provider.NAME.upper(), "BROKER_ACCOUNT"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def load_provider(name):
    return importlib.import_module(".providers." + name, __package__)


def main(provider_name, argv=None):
    provider = load_provider(provider_name)
    out.set_prefix(provider.CMD)
    argv = list(sys.argv[1:] if argv is None else argv)

    # `login`/`logout`/`update` are about the installation, not about running
    # work: they must reach the harness untouched, without picking an account.
    if argv and argv[0] in provider.PASSTHROUGH:
        return run.exec_passthrough(provider, argv)

    # OUR OWN subcommands come before the nested-call check. Inside a session
    # that already runs under the shim — a claude agent typing `broker-cl list`,
    # say — the marker is set, and treating that as a nested harness call sent
    # the word "list" to the harness itself, which answered "Not logged in".
    if argv and argv[0] in COMMANDS:
        module_name, func_name = COMMANDS[argv[0]]
        module = importlib.import_module("." + module_name, __package__)
        cfg = config.load(out.die)
        try:
            return getattr(module, func_name)(cfg, provider, argv[1:])
        except api.Unreachable as exc:
            # A dropped VPN is not a bug report. The run path has always
            # degraded to one line (select.py catches this); the subcommands
            # printed a hundred lines of traceback for the same blip.
            out.die("%s — check the network (VPN?) and try again" % exc)

    cfg = config.load(out.die)

    # A nested call: the harness spawning itself under a shim. Credentials and
    # profile are already in the environment we inherited, so re-resolving would
    # only cost a broker round-trip per subprocess — and for a provider whose
    # credentials ride in the environment, the child already holds the token.
    if os.environ.get(run.ACTIVE_ENV, "").startswith(provider.NAME + ":") and _still_holds_credentials(
        provider
    ):
        return run.exec_passthrough(provider, argv)

    explicit = None
    if argv and argv[0] == "account":
        rest = argv[1:]
        if not rest or rest[0] in ("-l", "--list", "list", "ls"):
            module = importlib.import_module(".commands", __package__)
            return module.cmd_list(cfg, provider, [])
        if rest[0] == "auto":
            argv = rest[1:]  # an explicit request for the pick we make anyway
        else:
            explicit, argv = rest[0], rest[1:]

    # CI pins an account through the environment (CODEX_ACCOUNT, AGY_ACCOUNT) when
    # it asks the broker directly, so the wrapper honours the same names — one way
    # to say "this account" whether you go through the CLI or through us. An
    # argument still wins, being the more explicit of the two.
    explicit = explicit or _account_from_env(provider)

    account, auth = select.resolve(cfg, provider, explicit)

    # The startup line goes in front of what you typed, so your own flags still
    # win: a later flag overrides an earlier one on every harness we wrap.
    from .commands import parse_startup

    startup_env, startup_argv = parse_startup((cfg.get("startup") or {}).get(provider.NAME))
    # medulla picks a model per node, and a panel depends on getting the model it
    # asked for. A startup line pinning ANTHROPIC_MODEL would quietly turn every
    # panellist into the same one, so an explicit --model wins over it.
    if any(a == "--model" or a.startswith("--model=") for a in argv):
        startup_env.pop("ANTHROPIC_MODEL", None)
        startup_argv = [a for a in startup_argv if not a.startswith("--model")]
    run.exec_harness(cfg, provider, account, auth, startup_argv + argv, startup_env)
    return 0
