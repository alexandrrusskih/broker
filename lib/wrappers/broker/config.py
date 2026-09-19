"""Local broker config — and the one place that knows where the broker keeps
things on this machine.

Every path below was once spelled out again wherever it was needed, which is how
`shim_previous` ended up being read from a file the rest of the package had
already stopped writing to. A path belongs here, and is imported from here.
"""

import json
import os
import tempfile

HOME = os.path.expanduser("~")

CONFIG_DIR = os.path.join(HOME, ".config", "broker")
CACHE_DIR = os.path.join(CONFIG_DIR, "cache")
SRC_CACHE = os.path.join(HOME, ".cache", "broker", "src")
ENGINE_DIR = os.path.join(HOME, ".local", "lib", "broker")
REAL_DIR = os.path.join(ENGINE_DIR, "real")

# $BROKER_CONFIG points the whole thing elsewhere — that is how a container or a
# self-hosted setup says where its config lives, and it wins over both paths below.
PATH = os.path.abspath(os.path.expanduser(
    os.environ.get("BROKER_CONFIG") or os.path.join(CONFIG_DIR, "config.json")))

# DROP AFTER 2026-12. The tool was called hltm-broker until September 2026. An
# install from back then is read once from the old place and copied across, so
# nobody has to re-run `broker config` after an update; new installs never touch
# this path.
LEGACY_PATH = os.path.join(HOME, ".config", "hltm-broker", "config.json")


def path():
    """The config file to read: the current one, or a copy of the old one."""
    if os.environ.get("BROKER_CONFIG") or os.path.exists(PATH) or not os.path.exists(LEGACY_PATH):
        return PATH
    try:
        os.makedirs(os.path.dirname(PATH), mode=0o700, exist_ok=True)
        import shutil

        shutil.copy2(LEGACY_PATH, PATH)
        # The config itself stays where it is — an older wrapper still on this
        # machine reads it. The credential cache beside it does NOT: it holds
        # access tokens, it is rebuilt on demand, and tokens nothing reads are
        # tokens sitting on disk for no reason.
        shutil.rmtree(os.path.join(os.path.dirname(LEGACY_PATH), "cache"), ignore_errors=True)
        return PATH
    except OSError:
        return LEGACY_PATH


DEFAULT_URL = os.environ.get("BROKER_URL")

# Stay on your own account while it has at least this much of its window left.
# Below it, look for room elsewhere. Override with "min_headroom" in the config.
MIN_HEADROOM = 20


def load(die):
    """Read the config, or explain what to run. `die` reports and exits."""
    try:
        with open(path()) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError) as exc:
        die("unreadable config %s: %s" % (path(), exc))
    # A container gets its key from the environment rather than a file.
    if not cfg.get("key") and os.environ.get("BROKER_KEY"):
        cfg["key"] = os.environ["BROKER_KEY"]
    if not cfg.get("key"):
        die("broker key missing — run 'broker config --url <broker-url> --key <broker_key>'")
    if not (cfg.get("url") or DEFAULT_URL):
        die("broker URL missing — run 'broker config --url <broker-url> --key <broker_key>'")
    cfg["url"] = url_of(cfg)
    return cfg


def url_of(cfg):
    """The broker's base URL, with old per-function bases migrated.

    A url saved before the broker collapsed into one function points at a base
    where every action now 404s; every action lives under /broker instead.
    """
    url = (cfg.get("url") or os.environ.get("BROKER_URL") or DEFAULT_URL).rstrip("/")
    if url.endswith("cloudfunctions.net"):
        url += "/broker"
    return url


def min_headroom(cfg):
    return cfg.get("min_headroom", MIN_HEADROOM)


def home_account(cfg, provider=None):
    """The account this machine belongs to for a provider.

    Accounts differ per provider (your codex is not your claude), so the
    per-provider map wins; the single `account` remains the fallback for setups
    written before it existed.
    """
    if provider:
        mapped = (cfg.get("accounts") or {}).get(provider)
        if mapped:
            return mapped
    return cfg.get("account")


def save(patch):
    """Merge into the config, atomically and 0600."""
    try:
        with open(path()) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    cfg.update(patch)
    directory = os.path.dirname(PATH)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
