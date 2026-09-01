"""Handing control to the real harness binary."""

import os
import sys

from . import profile
from .out import die, warn

SHIM_MARK = "hltm-broker shim"

# Set once the credentials for this run are in place. The harness spawns itself
# for sandboxed commands and helper processes, and under a shim every one of
# those re-entered the wrapper: a fresh broker call and usage probe per
# subprocess (280 of them in one observed panel run). Nested calls skip straight
# to the binary — the profile and the environment they inherit are already set.
ACTIVE_ENV = "HLTM_BROKER_ACTIVE"


def real_bin(provider):
    """The actual binary, never a shim.

    Once the native command is shimmed, resolving by name would loop back into
    us. Prefer the install's own path, and if that is missing, walk PATH skipping
    anything carrying our mark.
    """
    # A vendored platform binary beats a launcher script: launchers tend to
    # re-invoke the command by name, which under a shim never terminates.
    vendored = getattr(provider, "vendored_bins", lambda: [])()
    for candidate in list(vendored) + list(getattr(provider, "REAL_BINS", (provider.REAL_BIN,))):
        if candidate and os.access(candidate, os.X_OK) and not _is_shim(candidate):
            return candidate
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        candidate = os.path.join(directory, provider.BIN)
        if not os.access(candidate, os.X_OK) or os.path.isdir(candidate):
            continue
        try:
            with open(candidate, "rb") as fh:
                if SHIM_MARK.encode() in fh.read(512):
                    continue
        except OSError:
            continue
        # The install's own path was missing (a half-finished update, usually),
        # so this is a guess: it is merely the first thing named right. Say so —
        # running some unrelated program with the account's credentials in the
        # environment should never look like normal operation.
        warn(
            "%s is not where it should be (%s) — falling back to %s"
            % (provider.BIN, provider.REAL_BINS[0], candidate)
        )
        return candidate
    return None


def _is_shim(path):
    """Our own shim, wherever it turns up.

    The PATH walk always checked this; the provider's own list did not — and for
    claude the installed binary and the shim share one path, so the wrapper found
    the shim, ran it, and the shim called the wrapper again. A loop with no exit.
    """
    try:
        with open(path, "rb") as fh:
            return SHIM_MARK.encode() in fh.read(512)
    except OSError:
        return False


def _path_without_shim(provider, target):
    """A PATH where the harness's own name resolves to the real binary.

    The harness re-invokes itself by name for helper processes. Under a shim
    that lands back in this wrapper, which starts the harness, which re-invokes
    itself… — a loop that survives any in-process guard, because each turn is a
    fresh process doing something legitimate. So before handing over, put a
    directory first on PATH where `codex` IS the real binary.
    """
    shim_free = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hltm-real-%s-%d" % (provider.NAME, os.getuid())
    )
    try:
        os.makedirs(shim_free, mode=0o700, exist_ok=True)
        link = os.path.join(shim_free, provider.BIN)
        if os.path.realpath(link) != os.path.realpath(target):
            tmp = link + ".new"
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(target, tmp)
            os.replace(tmp, link)
    except OSError as exc:
        warn("could not shield the child PATH (%s) — nested calls may loop" % exc)
        return os.environ.get("PATH", "")
    return shim_free + os.pathsep + (os.environ.get("PATH") or "")


def exec_harness(cfg, provider, account, auth, argv, extra_env=None):
    """Install the credentials for this account and become the harness."""
    # Two ways a harness takes credentials. codex and agy read a file, so the
    # account gets a profile directory. claude reads an environment variable, so
    # there is nothing on disk to isolate — and therefore no profile at all, and
    # nothing of ~/.claude (MCP servers, history, projects) splits per account.
    if getattr(provider, "CREDENTIALS", "file") == "env":
        token = provider.env_token(auth)
        if not token:
            die("the broker returned no usable token for %s" % account)
        os.environ[provider.ENV_NAME] = token
        for name in getattr(provider, "CLEAR_ENV", ()):
            os.environ.pop(name, None)
        try:
            profile.write_cache(provider, account, auth)
        except OSError as exc:
            warn("could not cache the token (%s) — offline runs will not work" % exc)
    else:
        home = profile.profile_dir(provider, account)
        try:
            profile.write_auth(provider, home, auth)
        except OSError as exc:
            die("cannot write %s in %s: %s" % (provider.AUTH_NAME, home, exc))
        if provider.HOME_ENV:
            os.environ[provider.HOME_ENV] = home
    provider.route_refresh(os.environ, cfg, account, auth)
    harness_env = getattr(provider, "harness_env", None)
    if harness_env:
        harness_env(os.environ)
    for key, value in (extra_env or {}).items():
        os.environ[key] = value
    os.environ[ACTIVE_ENV] = "%s:%s" % (provider.NAME, account)

    target = real_bin(provider)
    if not target:
        die("cannot find the real %s — is it installed?" % provider.BIN)
    os.environ["PATH"] = _path_without_shim(provider, target)
    try:
        os.execv(target, [provider.BIN] + argv)
    except OSError as exc:
        die("cannot run %s: %s" % (target, exc))


def exec_passthrough(provider, argv):
    """Run a subcommand that must not touch account selection at all.

    `login`, `logout` and `update` are about the installation, not about running
    work: picking an account for them would write someone else's credentials into
    the profile, and a logout would then revoke the wrong account.
    """
    target = real_bin(provider)
    if not target:
        die("cannot find the real %s — is it installed?" % provider.BIN)
    os.environ["PATH"] = _path_without_shim(provider, target)
    os.execv(target, [provider.BIN] + argv)
    sys.exit(0)
