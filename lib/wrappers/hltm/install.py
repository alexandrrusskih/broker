"""Commands that change the installation: auth (login + seed) and upgrade."""

import json
import os
import re
import shutil
import subprocess
import sys

from . import accounts, api, profile, table
from .out import die, warn

# `upgrade` installs the CLI straight from the source repo — the only source
# there is. A fix to the wrapper is one push away, with no publish step to
# forget. Override per machine with "src_repo"/"src_subdir" in the broker config,
# or install from a checkout with --from <dir>.
SRC_REPO = "git@github.com:alexandrrusskih/broker.git"
SRC_SUBDIR = ""
SRC_CACHE = os.path.expanduser("~/.cache/hltm-broker/src")


def run(cmd):
    warn(" ".join(cmd))
    return subprocess.call(cmd)


def cmd_auth(cfg, provider, args):
    """Log an account in inside its own profile, then hand it to the broker.

    The harness refuses to start when its config dir does not exist, and a login
    in the canonical home would overwrite the auth file of whoever owns it — so
    the profile is created first, and login and seed both run against it.
    """
    names = [a for a in args if not a.startswith("-")]
    if not names:
        die("usage: %s auth <name> [--device|--browser]" % provider.CMD)
    account = names[0]
    if not re.match(r"^[A-Za-z0-9._-]+$", account):
        die("account name may only hold letters, digits, dot, dash or underscore")

    # Over SSH the login's localhost redirect cannot reach the browser, so the
    # device-code flow is the default there.
    device = "--device" in args or "--device-auth" in args
    if not device and "--browser" not in args:
        device = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))

    # A provider with no profile logs in wherever the user already is: creating a
    # directory for it would only leave an empty one behind.
    if getattr(provider, "CREDENTIALS", "file") == "env":
        home = provider.CANONICAL_HOME
        env = provider.login_env(dict(os.environ))
        # Over SSH there is no browser to open, and the attempt only adds noise
        # before the URL you actually need. `true` succeeds and does nothing.
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            env["BROWSER"] = "true"
    else:
        home = profile.profile_dir(provider, account)
        try:
            os.makedirs(home, mode=0o700, exist_ok=True)
            profile.prepare(provider, home)
        except OSError as exc:
            die("cannot create the profile %s: %s" % (home, exc))
        env = provider.login_env(dict(os.environ, **{provider.HOME_ENV: home}))
    login = provider.login_cmd(device)

    # Straight to the real binary, never through the name. The name is shimmed,
    # and the shim picks an account and hands it credentials — so a login command
    # that is not in PASSTHROUGH (agy has no `login` subcommand at all; it logs in
    # by running something that needs credentials) quietly ran as SOMEONE ELSE and
    # wrote no token, leaving the profile empty and the seed to fail on ENOENT.
    from .run import real_bin

    target = real_bin(provider)
    if not target:
        die("cannot find the real %s — is it installed?" % provider.BIN)
    login = [target] + login[1:]
    # Say which account this login is FOR. Printing the config dir made sense for
    # a provider with profiles; for one without, it named a directory that has
    # nothing to do with the account being created.
    if getattr(provider, "CREDENTIALS", "file") == "env":
        warn("%s   (its token will be seeded as account '%s')"
             % (" ".join([provider.BIN] + login[1:]), account))
    else:
        warn("%s   (%s=%s)" % (" ".join([provider.BIN] + login[1:]), provider.HOME_ENV, home))
    # A provider whose credentials travel in the environment has nothing on disk
    # to read afterwards: `claude setup-token` PRINTS the token, once. So the
    # output is echoed through to the terminal (the flow is interactive — it
    # shows a URL and waits for the pasted code) while being scanned for it.
    if getattr(provider, "CREDENTIALS", "file") == "env":
        minted, signed_in = _run_and_capture(login, env, getattr(provider, "TOKEN_RE", None))
        if signed_in and not minted:
            die("%s printed no token — nothing was seeded" % provider.BIN)
        if minted:
            env[provider.ENV_NAME] = minted
    else:
        signed_in = subprocess.call(login, env=env) == 0
    # The TUI exits 0 whether or not anyone signed in, so the profile is the only
    # honest answer to "did this work".
    if (signed_in and getattr(provider, "CREDENTIALS", "file") != "env"
            and not profile.read_auth(provider, account)):
        die("%s exited without leaving credentials in %s — nothing was seeded. "
            "Sign in fully (paste the code from the browser), then quit it."
            % (provider.BIN, home))
    if not signed_in:
        die("login did not complete — nothing was seeded. If %s printed an "
            "eligibility error, the sign-in itself worked and the account is the "
            "problem." % provider.BIN)

    if not shutil.which("broker"):
        die("broker CLI not in PATH — finish with 'broker seed %s --account %s'"
            % (provider.NAME, account))
    seed = ["broker", "seed", provider.NAME, "--account", account]
    warn(" ".join(seed))
    seed_ok = subprocess.call(seed, env=env) == 0

    swapped = (
        True
        if getattr(provider, "CREDENTIALS", "file") == "env"
        else _swap_for_broker_copy(cfg, provider, home, account)
    )
    if not seed_ok:
        die("seed failed — the broker does not own this account yet")
    if not swapped:
        # Seed worked but the swap did not: the broker owns rotation now, so the
        # real token must NOT stay on disk as a second, un-brokered holder.
        try:
            os.remove(os.path.join(home, provider.AUTH_NAME))
        except OSError:
            pass
        die("seeded, but could not render the broker's copy — removed the local "
            "%s so the real token isn't left behind; run '%s account %s' to finish"
            % (provider.AUTH_NAME, provider.CMD, account))

    row = accounts.probe(cfg, provider, account)
    if row["error"]:
        warn("seeded, but the broker cannot use it yet: %s" % row["error"])
        return 1
    warn(
        "%s ready — %s (%s), %s used, resets in %s"
        % (account, row["email"], row["plan"],
           "?" if row["used"] is None else "%d%%" % row["used"], table.human(row["resets_in"]))
    )
    return 0


def _run_and_capture(cmd, env, pattern):
    """Run an interactive login on a WIDE terminal, watching for a printed token.

    Two things have to be true at once. The login is a full-screen program, so it
    needs a terminal — on a pipe it redraws its spinner as a new line per frame
    and buries the URL. And the token it prints is 108 characters, so the
    terminal has to be wide enough to hold it on ONE line: a default-width pty
    wraps it, and a regex then matches only the first half. That produced a
    token that looked well-formed, seeded happily, and failed with 401 on first
    use. Hence an explicit 400-column window, and a length floor below.
    """
    import fcntl
    import pty
    import re
    import select
    import struct
    import termios
    import tty

    regex = re.compile(pattern or r"sk-ant-[A-Za-z0-9._-]{40,}")
    ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 400, 0, 0))
    process = subprocess.Popen(cmd, env=env, stdin=slave, stdout=slave, stderr=slave,
                               close_fds=True)
    os.close(slave)

    interactive = os.isatty(0)
    saved = termios.tcgetattr(0) if interactive else None
    seen, found = "", None
    try:
        if interactive:
            tty.setraw(0)
        while True:
            ready, _, _ = select.select([0, master] if interactive else [master], [], [], 0.2)
            if 0 in ready:
                data = os.read(0, 1024)
                if data:
                    os.write(master, data)
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                os.write(2, chunk)
                seen = (seen + ansi.sub("", chunk.decode("utf-8", "replace")))[-16384:]
                for match in regex.finditer(seen):
                    if found is None or len(match.group(0)) > len(found):
                        found = match.group(0)
            elif process.poll() is not None:
                break
    finally:
        if interactive and saved:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        os.close(master)
    return found, process.wait() == 0


def _swap_for_broker_copy(cfg, provider, home, account):
    """Replace the freshly-logged-in real token with the broker's rendering.

    The login wrote the REAL refresh token into this profile. Swap it BEFORE
    deciding the seed failed: if the broker took ownership but the verify call
    flaked, giving up here would strand the real token on disk.
    """
    for _try in range(2):
        try:
            profile.write_auth(provider, home, api.fetch_auth(cfg, provider, account))
            return True
        except Exception as exc:  # noqa: BLE001 — reported, then retried once
            last = exc
    warn("could not swap the local real token for the broker's copy: %s" % last)
    return False


def sync_source(cfg):
    """Clone or refresh the broker source; returns the package dir, or None.

    The checkout is ours alone (under ~/.cache), so a hard reset is safe — it
    never touches a working tree the user might have open elsewhere.
    """
    if not shutil.which("git"):
        warn("git not in PATH")
        return None
    repo = cfg.get("src_repo") or SRC_REPO
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
