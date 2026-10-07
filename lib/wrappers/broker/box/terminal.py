"""Handing the real terminal to a box, and taking it back.

Split out of run.py: the pty loop and the escape sequences that undo what a
harness leaves behind are a subject of their own, and they need nothing from the
rest of the box machinery — only the standard library.
"""

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import tty


def _through_terminal(cmd):
    """Run the box with its terminal intact, and read what goes past.

    The harness names the session itself as it exits — that line is the one
    true answer to "which conversation was this", and everything else the box
    has tried was a guess about file times in a directory every window writes
    to. So the line is read rather than reconstructed.

    Reading it must cost the terminal nothing: a full-screen interface needs a
    real tty on the other side, with its size, its signals and its resizes. So
    this is a pty in the middle, copying bytes both ways and keeping only the
    tail to search afterwards.

    Returns (exit status, what the harness printed).
    """
    import fcntl
    import signal
    import struct
    import termios
    import tty

    master, slave = pty.openpty()
    child = None
    last_size = None

    def resized(*_):
        nonlocal last_size
        try:
            size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            if size == last_size:
                return
            # The slave is closed after Popen; the master remains open for the run.
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
            last_size = size
            if child is not None:
                child.send_signal(signal.SIGWINCH)
        except (OSError, ValueError):
            pass

    resized()
    saved = None
    try:
        saved = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
    except (termios.error, ValueError, OSError):
        saved = None

    try:
        previous = signal.signal(signal.SIGWINCH, resized)
    except ValueError:
        previous = None

    # Ctrl-C belongs to whatever is inside: the terminal is raw, so it travels
    # as a byte down the pty and the harness decides what to do with it. This
    # process must not also die of it — someone holding the key down sends
    # several, and the ones after the first would kill the very thing that is
    # about to print how to come back. Seen exactly that: the harness named its
    # session, and nothing was left on the screen to say so.
    try:
        interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        interrupt = None
    child = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                             close_fds=True)
    os.close(slave)
    tail = b""
    try:
        while True:
            # Some terminal managers update the PTY size without SIGWINCH.
            resized()
            try:
                readable, _, _ = select.select([master, sys.stdin], [], [], 0.2)
            except (OSError, ValueError):
                break
            if master in readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                os.write(sys.stdout.fileno(), chunk)
                # Enough to hold the last screen, not enough to hold a session.
                tail = (tail + chunk)[-65536:]
            if sys.stdin in readable:
                try:
                    typed = os.read(sys.stdin.fileno(), 65536)
                except OSError:
                    typed = b""
                if typed:
                    os.write(master, typed)
            if child.poll() is not None and master not in readable:
                # It is gone, but the terminal may still hold the last of what
                # it wrote — the id among it. Drain to the end rather than stop
                # here: stopping here worked about one time in three, which is
                # the worst way for a thing to work.
                while True:
                    try:
                        rest = os.read(master, 65536)
                    except OSError:
                        break
                    if not rest:
                        break
                    os.write(sys.stdout.fileno(), rest)
                    tail = (tail + rest)[-65536:]
                break
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        if saved is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
            except (termios.error, ValueError, OSError):
                pass
        if previous is not None:
            try:
                signal.signal(signal.SIGWINCH, previous)
            except ValueError:
                pass
        if interrupt is not None:
            try:
                signal.signal(signal.SIGINT, interrupt)
            except ValueError:
                pass
    return child.wait(), tail
# Putting the terminal back the way the harness found it.
#
# A harness in a box owns the terminal completely: it switches to the alternate
# screen, asks for mouse reports, and turns on the kitty keyboard protocol, in
# which Enter arrives as "27;3u" and an arrow as "1:1A" rather than as ordinary
# characters. On the way out it undoes every one of those — but only if it gets
# to run. Stop the container from outside, or let it crash, and the process dies
# where it stands: the pane is left speaking a language the shell underneath
# does not understand, and typing into it produces "zsh: command not found: 1:1A".
#
# So the undoing belongs out here, in the thing that outlives the container.
# Sending these when they are already off costs nothing — each is a no-op on a
# terminal that is already in that state.
TERMINAL_RESET = (
    "\033[?1049l"                  # leave the alternate screen
    "\033[<u"                      # pop the kitty keyboard flags
    "\033[=0;1u"                   # ...and clear any that were set outright
    "\033[?1l\033>"                # cursor keys and keypad back to normal
    "\033[?2004l"                  # bracketed paste off
    "\033[?1004l"                  # focus in/out reporting off — it arrives as "\033[O"
    "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"  # mouse reporting off
    "\033[?25h"                    # cursor visible again
    "\033[0m"                      # attributes back to default
)
def _terminal_state():
    """This terminal's driver settings, to be restored after the run."""
    try:
        if not sys.stdin.isatty():
            return None
        return termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, OSError, ValueError):
        return None
def _restore_terminal(saved):
    """Undo both halves of what a harness does to a terminal: the driver's
    settings (raw mode, no echo) and the modes held by the emulator itself."""
    if saved is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        except (termios.error, OSError, ValueError):
            pass
    try:
        if sys.stdout.isatty():
            sys.stdout.write(TERMINAL_RESET)
            sys.stdout.flush()
    except (OSError, ValueError):
        pass
def _guard_terminal(saved):
    """Restore the terminal on the signals that would otherwise skip `finally`.

    A `finally` covers the ordinary endings — the harness exits, the container
    crashes, docker fails to start one. It does not cover this process being
    told to end: the default action for SIGTERM and SIGHUP is to die on the
    spot, leaving the pane in the harness's modes. SIGKILL still cannot be
    caught, and that is the one case left for `broker box repair`.
    """
    def handler(number, _frame):
        _restore_terminal(saved)
        # Exit the way the signal would have, so anything waiting on this
        # process still sees a signal death rather than a plain status.
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)

    for number in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):  # not the main thread, or no such signal
            pass
