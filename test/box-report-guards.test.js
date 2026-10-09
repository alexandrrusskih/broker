// What stops a box, or an overlapping launch, from breaking the report path.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { temp, engine } = require("./helpers");

const box = (dir, extra = {}) => ({ HOME: dir, HERDR_PANE_ID: "wA:p1", ...extra });

test("a box cannot name its own report directory", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(path.join(dir, ".claude"), { recursive: true });
  // A box's own "env" is documented to win over everything above it — and this
  // is the one variable it must not be able to choose. Pointed elsewhere, the
  // hook's reports go somewhere nobody reads, and the pane never wakes.
  await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({
    sneaky: { rw: [project], env: { AGNTBUS_SESSION_REPORT_DIR: "/tmp/mine" } },
  }));

  const out = engine(`
import os
from broker.box import boxes, report
boxes.PATH = "${path.join(dir, "boxes.json")}"
from broker.box.run import command
from broker.providers import claude
os.chdir("${project}")
cmd = command(claude, "sneaky", boxes.profiles()["sneaky"], ["--version"], {})
named = [p for p in cmd if p.startswith("AGNTBUS_SESSION_REPORT_DIR=")]
print("ALL", named)
# Docker takes the LAST -e for a repeated name, so the last word is the answer.
print("WINS", named[-1])
`, box(dir));

  assert.match(out, /WINS AGNTBUS_SESSION_REPORT_DIR=.*\/\.config\/broker\/box\/reports\/sneaky\//);
  assert.ok(!/WINS AGNTBUS_SESSION_REPORT_DIR=\/tmp\/mine/.test(out));
});

test("a report the box made enormous is refused, not read", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os
from broker.box import report, watch
from broker.providers import agy

report.flags("demo")
here = report.directory("demo")
watcher = watch.Watcher("demo", agy, None)
# The box writes this file. An unbounded read of it is this process's memory
# handed to whoever is inside.
with open(os.path.join(here, report.NAME), "wb") as handle:
    handle.write(b'{"agent":"antigravity","id":"' + b"a" * (4 * 1024 * 1024) + b'"}')
raw = watcher._read()
print("READ", len(raw), raw[:8])
print("TAKEN", watch.accept(raw.decode(), agy, None)[0])
`, box(dir));

  // Read small, refused by the same path as any other nonsense.
  assert.match(out, /READ 7 b'too big'/);
  assert.match(out, /TAKEN None/);
});

test("a symlink put there instead of a report is not followed", async (t) => {
  const dir = await temp(t);
  await fs.writeFile(path.join(dir, "elsewhere.json"),
    '{"agent":"antigravity","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}');
  const out = engine(`
import os
from broker.box import report, watch
from broker.providers import agy

report.flags("demo")
here = report.directory("demo")
os.symlink("${path.join(dir, "elsewhere.json")}", os.path.join(here, report.NAME))
watcher = watch.Watcher("demo", agy, None)
print("CHANGED", watcher._changed())
print("READ", watcher._read())
`, box(dir));

  // lstat sees it, so the tick notices something is there...
  assert.match(out, /CHANGED True/);
  // ...and the read refuses it rather than opening a file the box chose.
  assert.match(out, /READ None/);
});

test("a report the manager could not take is offered again", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os, sys, time
sys.stderr = sys.stdout
from broker.box import herdr, report, watch
from broker.providers import agy

watch.POLL = 0.02
attempts = []
def flaky(pane, provider, box, session):
    attempts.append(session)
    # The manager restarting is the one failure that matters, and it is also
    # exactly when it wants to be told everything.
    return (True, "") if len(attempts) > 2 else (False, "socket is not there")
herdr.tell = flaky

report.flags("demo")
watcher = watch.Watcher("demo", agy, None)
watcher._backoff = 0.02  # the real one backs off to 30s; this is the same path
watcher.start()
here = report.directory("demo")
temporary = os.path.join(here, ".tmp")
open(temporary, "w").write('{"agent":"antigravity","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}')
os.replace(temporary, os.path.join(here, report.NAME))
for _ in range(300):
    if watcher.sent:
        break
    time.sleep(0.02)
watcher.stop()
print("SENT", watcher.sent)
print("ATTEMPTS", len(attempts))
print("PENDING", watcher.pending)
`, box(dir));

  // The file is written once and never again; the old version remembered its
  // bytes as seen and so never offered the id a second time.
  assert.match(out, /SENT 0199aaaa-bbbb-cccc-dddd-eeeeffff0000/);
  assert.match(out, /PENDING None/);
  const attempts = Number(/ATTEMPTS (\d+)/.exec(out)[1]);
  assert.ok(attempts >= 3, `it kept trying (${attempts} attempts)`);
  // And it stops the moment the manager takes it.
  assert.ok(attempts <= 6, `it did not keep going afterwards (${attempts} attempts)`);
});

test("a run that is ending cannot delete the directory of one just starting", async (t) => {
  const dir = await temp(t);
  // Every cold restart overlaps the two: the pane is relaunched while the old
  // broker is still in its finally. With a path built from the window alone,
  // the old run's cleanup took the new run's directory with it.
  const out = engine(`
import os
from broker.box import report

new = report.directory("demo")
old = report.directory("demo", launch="99999-deadbeef")
os.makedirs(old, mode=0o700, exist_ok=True)
report.flags("demo")
print("DIFFERENT", new != old)
print("NEW", os.path.isdir(new))

# The old launcher, finishing: its release names its own launch, not the window.
import shutil
shutil.rmtree(old, ignore_errors=True)
try:
    os.rmdir(os.path.dirname(old))
except OSError:
    pass
print("SURVIVED", os.path.isdir(new))
`, box(dir));

  assert.match(out, /DIFFERENT True/);
  assert.match(out, /NEW True/);
  assert.match(out, /SURVIVED True/);
});

test("a launch never touches the directory of another launch", async (t) => {
  const dir = await temp(t);
  // A sweep by age was written here and taken out the same hour. A directory's
  // mtime only moves when something is added to it or removed, so a live box
  // sitting idle for a day keeps an old one — and a second launch in the same
  // pane would then delete the live mount of the first. codex-misc-p5 caught
  // it. Nothing sweeps now, and that is the point of this test.
  const out = engine(`
import os, time
from broker.box import report

idle = report.directory("demo", launch="1-idle-but-alive")
os.makedirs(idle, mode=0o700, exist_ok=True)
open(os.path.join(idle, report.NAME), "w").write("{}")
# A box that has said nothing for a week is still a box.
old = time.time() - 7 * 24 * 60 * 60
os.utime(idle, (old, old))

report.flags("demo")          # a second launch in the same pane
print("ALIVE", os.path.isdir(idle))
report.release("demo")        # and it ends
print("STILL", os.path.isdir(idle))
`, box(dir));

  assert.match(out, /ALIVE True/);
  assert.match(out, /STILL True/);
});
