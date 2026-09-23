#!/usr/bin/env python3
"""broker-oc — opencode in a box.

A launcher, and a narrow one: the broker holds no credentials for opencode and
chooses no account for it. This exists so that `opencode --box <name>` puts the
harness in a box; anything else goes straight through to opencode as installed. `broker wrap opencode` writes this file
and refreshes that package.

The engine is looked up rather than hard-coded, because this launcher also runs
inside containers whose $HOME differs from the host that wrapped it:

  $BROKER_ENGINE          explicit override
  ~/.local/lib/…          where `broker wrap` installs it, resolved at run time
  <path baked at wrap>    the wrapping host's copy
  importable as `broker`  already on sys.path (e.g. linked into site-packages)
"""

import os
import sys

CANDIDATES = (
    os.environ.get("BROKER_ENGINE"),
    os.path.expanduser("~/.local/lib/broker"),
    "__PKG_DIR__",
)

for _dir in CANDIDATES:
    if _dir and os.path.isdir(os.path.join(_dir, "broker")):
        sys.path.insert(0, _dir)
        break

try:
    from broker.cli import main
except ImportError as exc:  # noqa: BLE001 — the message is the whole point
    sys.exit(
        "broker-agy: cannot find the broker engine (%s).\n"
        "    looked in: %s\n"
        "    fix with: broker wrap agy   (or set $BROKER_ENGINE)"
        % (exc, ", ".join(d for d in CANDIDATES if d))
    )

if __name__ == "__main__":
    try:
        sys.exit(main("opencode"))
    except KeyboardInterrupt:
        sys.exit(130)
