// The terminal around a box, and how much room the machine has left.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const root = path.join(__dirname, "..");

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-box-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  // realpath: on macOS the temp directory sits under /var, which is itself a
  // symlink to /private/var — and the code under test resolves symlinks.
  return fs.realpath(dir);
}

// The engine builds the command line; running python is how we see it.
function engine(code, env = {}) {
  return execFileSync("python3", ["-c", `import sys; sys.path.insert(0, 'lib/wrappers')\n${code}`],
    { cwd: root, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", ...env } });
}

test("a box puts the terminal back, whatever killed it", () => {
  // A harness in a box switches the terminal to the alternate screen, asks for
  // mouse reports and turns on the kitty keyboard protocol. Killed outright it
  // undoes none of it, and the shell underneath then reads Enter as "27;3u".
  const out = engine(`
import ast, io, json, sys
from broker.box import terminal

wrote = io.StringIO()


class Tty(io.StringIO):
    def isatty(self):
        return True


# What the sequence actually turns off.
reset = terminal.TERMINAL_RESET
modes = {
    "alternate screen": "\\033[?1049l" in reset,
    "kitty keyboard": "\\033[<u" in reset,
    "cursor keys": "\\033[?1l" in reset,
    "bracketed paste": "\\033[?2004l" in reset,
    "mouse": "\\033[?1000l" in reset and "\\033[?1006l" in reset,
    "cursor shown": "\\033[?25h" in reset,
}

# On a terminal it is written; on a pipe it is not, or a redirected run would
# collect escape bytes in its output file.
tty, sys.stdout = sys.stdout, Tty()
terminal._restore_terminal(None)
on_tty = sys.stdout.getvalue()
sys.stdout = io.StringIO()
terminal._restore_terminal(None)
on_pipe = sys.stdout.getvalue()
sys.stdout = tty

# The restore has to sit in a finally, or an exception on the way out skips it.
tree = ast.parse(io.open("lib/wrappers/broker/box/start.py", encoding="utf-8").read())
# The signal handlers live in box/terminal.py with the pty loop; exec_box, and
# therefore the finally that restores the terminal, lives in box/start.py.
signals_tree = ast.parse(
    io.open("lib/wrappers/broker/box/terminal.py", encoding="utf-8").read())
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "exec_box")
guarded = any(
    any("_restore_terminal" == getattr(getattr(c, "func", None), "id", None)
        for c in ast.walk(ast.Module(body=node.finalbody, type_ignores=[])))
    for node in ast.walk(fn) if isinstance(node, ast.Try) and node.finalbody
)

print(json.dumps({
    "modes": modes,
    "on_tty": on_tty == reset,
    "on_pipe": on_pipe,
    "guarded": guarded,
    "signals": sorted(
        n.attr for n in ast.walk(signals_tree)
        if isinstance(n, ast.Attribute) and n.attr in ("SIGTERM", "SIGHUP")
    ),
}))
`);
  const got = JSON.parse(out);
  for (const [mode, present] of Object.entries(got.modes)) {
    assert.equal(present, true, `the reset must turn off ${mode}`);
  }
  assert.equal(got.on_tty, true, "a terminal gets the full sequence");
  assert.equal(got.on_pipe, "", "a pipe gets nothing — no escapes in a log file");
  assert.equal(got.guarded, true, "exec_box must restore the terminal in a finally");
  // SIGKILL cannot be caught; 'broker box repair' is the way back from that one.
  assert.deepEqual(got.signals, ["SIGHUP", "SIGTERM"]);
});

test("how much room the machine has left for heavy work", async (t) => {
  const dir = await temp(t);
  const slots = path.join(dir, "slots");
  await fs.mkdir(slots, { recursive: true });
  await fs.writeFile(path.join(slots, "slot1"), "");
  await fs.writeFile(path.join(slots, "slot2"), "");
  await fs.writeFile(path.join(slots, "slot1.owner"), "p4 12:00");
  // Whatever else people leave in there is not a slot.
  await fs.writeFile(path.join(slots, "notes.json"), "{}");

  const out = engine(`
import json
from broker.box import slots
free, total, busy = slots.state(${JSON.stringify(slots)})
print(json.dumps({"free": free, "total": total, "busy": busy}))
`);
  const idle = JSON.parse(out);
  assert.deepEqual(idle, { free: 2, total: 2, busy: [] },
    "two slots, nobody holding one — and the stray file is not counted");

  // Now hold one the way a heavy run does, and ask again from another process.
  const held = engine(`
import fcntl, json, os, subprocess, sys
handle = open(os.path.join(${JSON.stringify(slots)}, "slot1"), "a+")
fcntl.flock(handle, fcntl.LOCK_EX)
out = subprocess.run([sys.executable, "-c",
    "import sys, json; sys.path.insert(0, sys.argv[1]);"
    "from broker.box import slots;"
    "f, t, b = slots.state(sys.argv[2]); print(json.dumps({'free': f, 'busy': b}))",
    ${JSON.stringify(path.join(root, "lib", "wrappers"))}, ${JSON.stringify(slots)}],
    capture_output=True, text=True)
fcntl.flock(handle, fcntl.LOCK_UN)
print(out.stdout.strip())
`);
  const seen = JSON.parse(held);
  assert.equal(seen.free, 1, "one slot is taken, so one is left");
  assert.deepEqual(seen.busy, ["slot1"]);
  // Asking must not cost a slot: the lock is taken and released at once.
  const after = JSON.parse(engine(`
import json
from broker.box import slots
free, total, _ = slots.state(${JSON.stringify(slots)})
print(json.dumps({"free": free}))
`));
  assert.equal(after.free, 2, "asking the question leaves nothing held");
});
