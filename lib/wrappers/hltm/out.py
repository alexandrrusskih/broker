"""Messages to the operator. Everything goes to stderr so it never mixes into
whatever the harness itself prints on stdout."""

import sys

# Replaced by set_prefix() with the wrapper's own name on every real run. The
# default only shows on a path that forgot to set it — so it names the package,
# not `cx`, which was renamed to broker-cx and no longer exists anywhere.
PREFIX = "hltm-broker"


def set_prefix(name):
    global PREFIX
    PREFIX = name


def warn(msg):
    print("%s: %s" % (PREFIX, msg), file=sys.stderr)


def die(msg, code=1):
    warn(msg)
    sys.exit(code)
