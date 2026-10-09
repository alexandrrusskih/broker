"""Starting a box: the terminal here, the container there."""

import os
import shlex
import shutil
import subprocess
import sys
import time

from .. import config
from ..out import die, warn
from . import boxes, state
from .run import command
from .sync import _sync_back
from .sessions import _exit_note, _pin_session, _session_from_argv, _session_it_named
from .store import _remember, _resume_hint, _session_from_store
from .terminal import (_guard_terminal, _restore_terminal, _terminal_state,
                       _through_terminal)
from .paths import expand


def _daemon_is_up(binary):
    """Whether this runtime's daemon answers at all.

    `version` and not `info`: it is the one call that talks to the server and
    returns before anything is enumerated — 56ms here against a live daemon,
    and a non-zero exit the moment the socket is dead.
    """
    try:
        probe = subprocess.run([binary, "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and bool(probe.stdout.strip())


def _over_ssh(cmd, machine, name):
    """The same container command, run on another machine instead of this one.

    Only the terminal stays here. The image, the project and the harness are all
    over there, so the paths in the command are that machine's paths — a box
    describes what it may touch, and a machine says where those things live on
    it. Anything the machine does not redirect is passed through unchanged,
    which is right when both sides keep a project in the same place and wrong
    silently when they do not, so a machine that differs must say so.
    """
    swaps = sorted((machine.get("paths") or {}).items(), key=lambda kv: -len(kv[0]))

    def moved(value):
        for mine, theirs in swaps:
            mine = expand(mine)
            if value == mine or value.startswith(mine + os.sep):
                return expand(theirs) + value[len(mine):]
        return value

    # ONLY the source half of a bind mount is a path on the other machine.
    # Everything else that looks like a path — the target of a mount, the
    # working directory, a tmpfs, $HOME — is a path INSIDE the container, and
    # the whole point of a box is that those stay the same wherever it runs.
    # Rewriting them would move the box's own furniture and break --resume.
    out = []
    for part in cmd:
        if part.startswith("type=bind,source="):
            head, _, rest = part.partition("source=")
            source, sep, tail = rest.partition(",")
            out.append(head + "source=" + moved(source) + sep + tail)
        else:
            out.append(part)

    target = machine.get("ssh")
    if not target:
        die("machine '%s' has no \"ssh\" target in %s" % (name, boxes.PATH))
    # -t: a harness is a full-screen program and needs a terminal on the far
    # side. Without it the pane comes up in line mode and nothing redraws.
    ssh = ["ssh", "-t", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=120", target]
    return ssh + ["--"] + [shlex.quote(part) for part in out]


def exec_box(provider, name, argv, env, account=None):
    """Run the container, then say how to come back to it."""
    defined = boxes.profiles()
    if name not in defined:
        die("no box called '%s' in %s%s" % (
            name, boxes.PATH,
            (" — defined: " + ", ".join(sorted(defined))) if defined else " (the file does not exist yet)"))
    # A terminal manager watches the pane's foreground process to know what runs
    # in it. From here on that process is `docker`, which hides the harness
    # behind it — Herdr documents the way out: a wrapper says which agent it
    # stands for, and the manager reads its screen as it would any other.
    if os.environ.get("HERDR_PANE_ID") and not os.environ.get("HERDR_AGENT"):
        os.environ["HERDR_AGENT"] = provider.NAME

    remote, machine, argv = boxes.take_remote(argv)
    # Asked for before anything is built on the assumption it is there. Without
    # this, a stopped Docker produced three messages and no answer: the registry
    # cache warned (it is never allowed to be fatal), `docker run` printed the
    # daemon's own "Cannot connect" line, and then this function said "Resume it
    # in this box with: …" — the command that had just failed, offered as the way
    # back into a box that never came up.
    #
    # Only for a local run: a remote box's daemon is on the far machine, and the
    # one here may well be stopped and irrelevant.
    if not remote:
        runtime = defined[name].get("runtime") or "docker"
        binary = shutil.which(runtime)
        if binary and not _daemon_is_up(binary):
            die("the %s daemon is not running — start it, then try again" % runtime)
    pinned, argv = _pin_session(provider, argv)
    # Which pane this box belongs to, and the chat in it when we named it.
    state.claim(name, provider, pinned, env)
    cmd = command(provider, name, defined[name], argv, env, remote=bool(remote))
    shadow_root = os.path.join(config.CONFIG_DIR, "box", "profiles") + os.sep
    shadows = [part.partition("source=")[2].partition(",")[0]
               for part in cmd if part.startswith("type=bind,source=")
               and part.partition("source=")[2].startswith(shadow_root)]
    if remote:
        # Sessions, databases and MCP bridges all belong to the machine the box
        # runs on, and none of them reach across. Say so once, rather than let
        # it be discovered by something behaving oddly.
        warn("box '%s' runs on %s: its sessions and bridged MCP servers live there, not here"
             % (name, remote))
        cmd = _over_ssh(cmd, machine, remote)
    workdir = cmd[cmd.index("-w") + 1] if "-w" in cmd else os.getcwd()
    started = time.time()

    # Waited for rather than exec'd into, only so the box can add its own line
    # after the harness has printed its resume hint. Everything else about the
    # run is unchanged: stdio is inherited, so the terminal, the mouse and the
    # clipboard behave as if nothing sat in between, and `docker run -it`
    # forwards the signals itself.
    saved = _terminal_state()
    _guard_terminal(saved)
    status = 0
    printed = b""
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            # Watched, so the id the harness names on its way out can be read
            # rather than guessed at afterwards.
            status, printed = _through_terminal(cmd)
        else:
            status = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        # Ctrl-C reaches this process as well as the container. The harness
        # inside still exits properly and its session file is complete, so the
        # rest of this — folding the session back, saying how to return — is
        # exactly as valuable as after an ordinary exit.
        status = 130
    except OSError as exc:
        die("cannot start the '%s' box: %s" % (name, exc))
    finally:
        # Whatever happened in there — a clean exit, a crash, `docker stop` from
        # another window — the pane is usable again from this line on.
        _restore_terminal(saved)
        for shadow in shadows:
            shutil.rmtree(shadow, ignore_errors=True)
        # The pane this box belonged to, and whatever it said about its chat.
        # Left behind, both would name a conversation that has ended.
        state.release(name)

    # 125 is the one exit code docker keeps for itself: the CLI could not run the
    # container at all. Nothing ran in there, so there is no session to fold back
    # and nothing to resume — and everything below this line is about a box that
    # held a conversation. Said as a failure instead, which is what it is.
    if status == 125 and not printed.strip():
        _remember(provider, name, workdir, None, account, status)
        die("the '%s' box did not start — the reason is above" % name, status)

    # What the harness itself recorded, where it could not be confused with
    # another window's — better than any guess made from file times.
    # What the harness said, in its own words, beats anything worked out from
    # file times in a directory every window writes into.
    # Only what the harness itself named. There used to be a fallback that
    # worked the id out from file times, and it was worse than nothing: every
    # window on this machine writes its sessions into one directory, so the
    # "newest file" belongs to whoever typed last. Two boxes started together
    # were handed the same id, and it belonged to neither. A printed id has to
    # mean something; when there is none, say so.
    # Two sources, both of them plain fact: what the harness printed about
    # itself, and what was typed to start it. A third was tried — digging the
    # thread id out of the harness's own log — and it came back with the middle
    # of a sentence. Anything that has to be checked for being nonsense before
    # it can be printed is not a source, it is a guess with paperwork.
    session = (pinned
               or _session_it_named(printed, getattr(provider, "SESSION_PRINTED", None), provider)
               or _session_from_argv(provider, argv))
    _remember(provider, name, workdir, session, account, status)
    if session is None:
        session = _session_from_store(provider, name, started)
    # Said whatever happened, including when there is no id to say: a box that
    # goes quiet leaves someone staring at a prompt wondering what became of an
    # hour's conversation. Without an id the line points at the harness's own
    # picker, which is a poorer answer than the id and a far better one than
    # silence. This used to sit behind "if we know the session", so the very
    # case that needed saying out loud was the one that said nothing.
    #
    # Printed BEFORE the session is folded back: folding asks the harness to
    # re-read its own session, and it looks through every session it has to
    # find the one named — thirteen thousand of them here, which is seconds of
    # silence. The line is what the person is waiting for; the bookkeeping can
    # happen behind it.
    #
    # Registered as well as printed: whatever finishes this process — a signal
    # that got through, an error on the way out — the line still goes. It is
    # the only place the id exists once the screen has been restored.
    import atexit

    printed_once = []

    def say(text):
        if text and text not in printed_once:
            printed_once.append(text)
            try:
                # stderr, like every other message to the operator — out.py says
                # why in its first line: stdout belongs to the harness. This one
                # line broke that rule, and it is the line that shows it. The pty
                # loop above writes the harness's bytes straight to fd 1 with
                # os.write, unbuffered, while this wrote through the buffered
                # sys.stdout layer on the SAME descriptor; the two arrived spliced
                # mid-word, the harness's own resume line tangled into ours.
                # fd 1 is flushed first so what the harness said still lands
                # before what we say about it.
                sys.stdout.flush()
                sys.stderr.write(text)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass

    note = _exit_note(provider, session, workdir, env)
    if note:
        warn("the harness stopped with: %s" % " ".join(note.split()))
    hint = _resume_hint(provider, name, workdir, env, started, account, session)
    atexit.register(say, hint)
    say(hint)
    if session:
        _sync_back(provider, session, env, name)
    sys.exit(status)
