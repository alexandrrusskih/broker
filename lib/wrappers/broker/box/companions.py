"""The other harnesses a box carries, so the one inside can start them too.

A box is built around the harness that started it: its directory, its account,
its credentials. Everything else in the image was a binary that could not log
in — medulla in an agy box ran its claude and codex nodes into "Not logged in",
and a panel of models was a panel of one. A box that says

    "harnesses": ["claude", "codex", "agy", "opencode"]

gets each of them the way it would get it as its own harness. Nothing changes
for a box that does not say it.
"""

import contextlib
import importlib
import os

from .. import out
from .paths import REAL_HOME_ENV

KEY = "harnesses"
KNOWN = ("claude", "codex", "agy", "opencode")


@contextlib.contextmanager
def _real_home():
    """$HOME as it was before a harness took it over.

    agy and opencode are handed their profile AS $HOME, and a provider module
    works its paths out from "~" when it is first imported — which, for a
    companion, is now. Imported under the profile, codex looked for itself in
    ~/.agy/<account>/.codex.
    """
    real = os.environ.get(REAL_HOME_ENV)
    if not real or os.environ.get("HOME") == real:
        yield
        return
    taken = os.environ["HOME"]
    os.environ["HOME"] = real
    try:
        yield
    finally:
        os.environ["HOME"] = taken


def companions(provider, profile, env):
    """[(provider, env)] for every other harness this box carries.

    Each env is a copy of the one this run was given, with that harness's own
    account installed in it — resolved here, on the host, exactly as a run of
    that harness would resolve it. A harness that cannot get an account is
    left out with a line saying why, rather than taking the box down with it:
    the box was asked for, the companions are extra.
    """
    names = profile.get(KEY)
    if not names:
        return []
    if not isinstance(names, list) or any(name not in KNOWN for name in names):
        out.die('"%s" lists the harnesses a box carries besides its own: %s'
                % (KEY, ", ".join(KNOWN)))
    from .. import config, run, select
    from ..cli import _account_from_env

    found = []
    cfg = None
    prefix = out.PREFIX
    with _real_home():
        for name in names:
            if name == provider.NAME or any(p.NAME == name for p, _ in found):
                continue
            other = importlib.import_module("..providers." + name, __package__)
            own = dict(env)
            # Its account lines carry its own name, not the box's harness's.
            out.set_prefix(other.CMD)
            try:
                if getattr(other, "CREDENTIALS", "file") is not None:
                    cfg = cfg or config.load(out.die)
                    account, auth = select.resolve(cfg, other, _account_from_env(other))
                    run.install_credentials(cfg, other, account, auth, own)
                    own[run.ACTIVE_ENV] = "%s:%s" % (other.NAME, account)
            except SystemExit:
                # die() has already said what went wrong.
                out.warn("the box goes on without %s" % name)
                continue
            finally:
                out.set_prefix(prefix)
            found.append((other, own))
    return found
