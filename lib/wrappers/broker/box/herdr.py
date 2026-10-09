"""Telling the terminal manager which chat is in a pane, and being believed.

Split out of box/report.py, which is about the directory a box writes into.
This is the other half: the calls out to the manager over its own socket, and
reading what it answers instead of assuming.

Nothing here trusts the word "ok". The manager answers `{"result":{"type":
"ok"}}` to a report it accepted AND to one it dropped — codex-misc-p5 proved
both against a live socket: a pane already held by one source kept its own
agent and id while the conflicting report was answered ok. So a report is
followed by a read of the pane, and only the pane itself settles whether it
landed.
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

# Long enough for a busy manager, short enough that a dead socket does not hold
# a box's exit. Reads are retried by the caller, so patience is not needed here.
TIMEOUT = 2


def resume_argv(provider, box, session):
    """What to run to open this chat again, built HERE.

    Never taken from the report. A box that could hand out a command line would
    be handing it to whatever reruns the pane, outside any container.
    """
    form = getattr(provider, "SESSION_RESUME", "--resume %s")
    return [provider.BIN] + shlex.split(form % session) + ["--box", box]


def call(method, params):
    """One request, one line back. (reply, "") or (None, why not)."""
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        return None, "this pane has no manager socket"
    request = {
        "id": "%s:%d:%06d" % (SOURCE, int(time.time() * 1000), random.randrange(1_000_000)),
        "method": method,
        "params": params,
    }
    answer = b""
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(TIMEOUT)
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
        return None, str(exc)
    return accepted(answer.split(b"\n", 1)[0].decode("utf-8", "replace"))


def accepted(answer):
    """The result in this answer, or None and the reason there is none.

    Fails closed on purpose. The first version of this returned success as soon
    as the socket had been read at all — so a refusal was recorded as a report
    delivered, and the pane went on believing nothing while this process
    believed it had said everything.
    """
    if not answer:
        return None, "it answered nothing"
    try:
        reply = json.loads(answer)
    except ValueError:
        return None, "its answer was not JSON"
    if not isinstance(reply, dict):
        return None, "its answer was not an object"
    if reply.get("error") is not None:
        error = reply["error"]
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or error
        return None, str(error)
    result = reply.get("result")
    if result is None:
        return None, "its answer carried no result"
    if isinstance(result, dict) and (result.get("accepted") is False or result.get("error")):
        return None, str(result.get("reason") or result.get("error") or "it refused")
    return result, ""


def holder(pane):
    """What the pane says is in it: (agent, source, id), or None and why not."""
    result, why = call("pane.get", {"pane_id": pane})
    if result is None:
        return None, why
    found = (result or {}).get("pane") if isinstance(result, dict) else None
    if not isinstance(found, dict):
        return None, "it did not describe the pane"
    session = found.get("agent_session")
    if not isinstance(session, dict):
        return None, "the pane holds no session"
    return (session.get("agent"), session.get("source"), session.get("value")), ""


def tell(pane, provider, box, session):
    """Hand the manager the id and the way back, then check that it took it.

    `pane.report_agent`, and not `pane.report_agent_session`, which is the
    narrower call: the manager takes a session from a source only once that
    source already holds the pane's agent. The broker never does on a first
    report, so that call was refused every time. This one says who is in the
    pane and which chat it is together, which is the whole of what is true.

    The state is `idle` because that is what it is: the report lands when the
    harness has just said something to the bus, which is the harness waiting.
    """
    _, why = call("pane.report_agent", {
        "pane_id": pane,
        "source": SOURCE,
        "agent": provider.NAME,
        "seq": time.time_ns(),
        "state": "idle",
        "agent_session_id": session,
        # The only reason any of this survives a restart of the manager: it
        # kills the pane's terminal, the launcher and the container, so the chat
        # is not resumed but RELAUNCHED, and only the manager can remember with
        # what.
        "resume_argv": resume_argv(provider, box, session),
    })
    if why:
        return False, why
    # The answer was ok. That is not the same as applied, so ask the pane.
    held, why = holder(pane)
    if held is None:
        return False, "it answered ok and then %s" % why
    agent, source, value = held
    if source != SOURCE or agent != provider.NAME or value != session:
        return False, ("it answered ok but the pane still holds %s/%s %s"
                       % (source, agent, value))
    return True, ""
