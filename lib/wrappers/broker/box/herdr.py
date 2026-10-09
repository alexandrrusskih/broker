"""Telling the terminal manager which chat is in a pane, and being believed.

Split out of box/report.py, which is about the directory a box writes into.
This is the other half: one call out to the manager over its own socket, and
reading what it answers instead of assuming.
"""

import json
import os
import random
import shlex
import socket
import time


# Said plainly, and never one of the manager's own names: a report from here is
# a report from the broker, and a manager that does not know the broker should
# refuse it rather than be fooled into trusting it.
SOURCE = "broker:box"


def resume_argv(provider, box, session):
    """What to run to open this chat again, built HERE.

    Never taken from the report. A box that could hand out a command line would
    be handing it to whatever reruns the pane, outside any container.
    """
    form = getattr(provider, "SESSION_RESUME", "--resume %s")
    return [provider.BIN] + shlex.split(form % session) + ["--box", box]


def accepted(answer):
    """Whether the manager said it took the report, and why not when it did not.

    Fails closed on purpose. The first version of this sent the report and
    returned success as soon as the socket had been read at all — so a refusal
    was recorded as a report delivered, and the pane went on believing nothing
    while this process believed it had said everything. codex-misc-p5 found it
    by reading the manager's own source: a report from a source it does not
    already trust for that pane is answered with session_not_accepted.
    """
    if not answer:
        return False, "it answered nothing"
    try:
        reply = json.loads(answer)
    except ValueError:
        return False, "its answer was not JSON"
    if not isinstance(reply, dict):
        return False, "its answer was not an object"
    if reply.get("error") is not None:
        error = reply["error"]
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or error
        return False, str(error)
    result = reply.get("result")
    if isinstance(result, dict):
        if result.get("accepted") is False or result.get("error"):
            return False, str(result.get("reason") or result.get("error") or "it refused")
    elif result is None:
        return False, "its answer carried no result"
    return True, ""


def tell(pane, provider, box, session):
    """Hand the manager the id and the way back, over its own socket.

    `pane.report_agent`, and not `pane.report_agent_session`, which is the
    narrower call: the manager takes a session from a source only once that
    source already holds the pane's agent. The broker never does on a first
    report, so that call was refused every time. This one says who is in the
    pane and which chat it is together, which is the whole of what is true.

    The state is `idle` because that is what it is: the report lands when the
    harness has just said something to the bus, which is the harness waiting.
    """
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        return False, "this pane has no manager socket"
    request = {
        "id": "%s:%d:%06d" % (SOURCE, int(time.time() * 1000), random.randrange(1_000_000)),
        "method": "pane.report_agent",
        "params": {
            "pane_id": pane,
            "source": SOURCE,
            "agent": provider.NAME,
            "seq": time.time_ns(),
            "state": "idle",
            "agent_session_id": session,
            # The only reason any of this survives a restart of the manager: it
            # kills the pane's terminal, the launcher and the container, so the
            # chat is not resumed but RELAUNCHED, and only the manager can
            # remember with what.
            "resume_argv": resume_argv(provider, box, session),
        },
    }
    answer = b""
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        try:
            client.connect(path)
            client.sendall((json.dumps(request) + "\n").encode())
            # One line is the whole answer. Read until it arrives rather than
            # once: a refusal is longer than a confirmation, and half of it
            # parses as nothing.
            while b"\n" not in answer:
                chunk = client.recv(4096)
                if not chunk:
                    break
                answer += chunk
        finally:
            client.close()
    except OSError as exc:
        return False, str(exc)
    return accepted(answer.split(b"\n", 1)[0].decode("utf-8", "replace"))
