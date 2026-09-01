#!/usr/bin/env python3
"""cx — codex through the hltm token-broker.

A launcher, nothing else: the engine lives in the hltm package, so a fix is a
package file rather than a 900-line script. `broker wrap codex` writes this file
and refreshes that package.

The engine is looked up rather than hard-coded, because this launcher also runs
inside containers whose $HOME differs from the host that wrapped it:

  $HLTM_ENGINE            explicit override
  ~/.local/lib/…          where `broker wrap` installs it, resolved at run time
  <path baked at wrap>    the wrapping host's copy
  importable as `hltm`    already on sys.path (e.g. linked into site-packages)
"""

import os
import sys

CANDIDATES = (
    os.environ.get("HLTM_ENGINE"),
    os.path.expanduser("~/.local/lib/hltm-broker"),
    "__PKG_DIR__",
)

for _dir in CANDIDATES:
    if _dir and os.path.isdir(os.path.join(_dir, "hltm")):
        sys.path.insert(0, _dir)
        break

try:
    from hltm.cli import main
except ImportError as exc:  # noqa: BLE001 — the message is the whole point
    sys.exit(
        "cx: cannot find the hltm engine (%s).\n"
        "    looked in: %s\n"
        "    fix with: broker wrap codex   (or set $HLTM_ENGINE)"
        % (exc, ", ".join(d for d in CANDIDATES if d))
    )

if __name__ == "__main__":
    try:
        sys.exit(main("codex"))
    except KeyboardInterrupt:
        sys.exit(130)
