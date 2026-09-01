"""Local broker config: ~/.config/hltm-broker/config.json."""

import json
import os
import tempfile

PATH = os.path.expanduser("~/.config/hltm-broker/config.json")
DEFAULT_URL = os.environ.get("BROKER_URL")

# Stay on your own account while it has at least this much of its window left.
# Below it, look for room elsewhere. Override with "min_headroom" in the config.
MIN_HEADROOM = 20


def load(die):
    """Read the config, or explain what to run. `die` reports and exits."""
    try:
        with open(PATH) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        die("broker not configured — run 'broker config --key <broker_key>'")
    except (OSError, ValueError) as exc:
        die("unreadable config %s: %s" % (PATH, exc))
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
    url = (cfg.get("url") or DEFAULT_URL).rstrip("/")
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
        with open(PATH) as fh:
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
