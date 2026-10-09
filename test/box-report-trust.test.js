// What a box is believed about, and what it is refused. The directory it
// writes into is in box-report.test.js; this file is about the contents.
const test = require("node:test");
const assert = require("node:assert/strict");
const { temp, engine } = require("./helpers");

// HOME decides where the broker keeps its own files, so every test here gets
// its own machine. Patching an attribute instead was tried and it leaked.
const box = (dir, extra = {}) => ({ HOME: dir, HERDR_PANE_ID: "wA:p1", ...extra });


test("what a report has to be before it is believed", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import herdr, report, watch
from broker.providers import agy, claude

good = '{"agent":"claude","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}'
cases = [
  ("good", claude, None, good),
  ("pinned-match", claude, "0199aaaa-bbbb-cccc-dddd-eeeeffff0000", good),
  ("pinned-other", claude, "0199ffff-0000-1111-2222-333344445555", good),
  ("wrong-harness", agy, None, good),
  ("antigravity-is-agy", agy, None, '{"agent":"antigravity","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}'),
  ("short-id", claude, None, '{"agent":"claude","id":"tooshort"}'),
  ("half-written", claude, None, '{"agent":"clau'),
  ("not-an-object", claude, None, '"claude"'),
  ("id-with-a-slash", claude, None, '{"agent":"claude","id":"../../etc/passwd-aaaaaaaa"}'),
]
for label, provider, pinned, raw in cases:
    print(label, watch.accept(raw, provider, pinned)[0])
`, box(dir));

  assert.match(out, /^good 0199aaaa-bbbb-cccc-dddd-eeeeffff0000$/m);
  assert.match(out, /^pinned-match 0199aaaa/m);
  // claude's id is chosen out here BEFORE the container starts, so a box that
  // names any other chat of the same harness is refused outright.
  assert.match(out, /^pinned-other None$/m);
  assert.match(out, /^wrong-harness None$/m);
  // The bus hook for agy says "antigravity"; the canonical label is "agy".
  assert.match(out, /^antigravity-is-agy 0199aaaa/m);
  assert.match(out, /^short-id None$/m);
  assert.match(out, /^half-written None$/m);
  assert.match(out, /^not-an-object None$/m);
  assert.match(out, /^id-with-a-slash None$/m);
});

test("the way back is built here, never taken from the box", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import herdr, report, watch
from broker.providers import agy, claude, codex
for provider in (claude, codex, agy):
    print(provider.NAME, herdr.resume_argv(provider, "joppa", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"))
`, box(dir));
  assert.match(out, /claude \['claude', '--resume', '0199aaaa[^']*', '--box', 'joppa'\]/);
  assert.match(out, /codex \['codex', 'resume', '0199aaaa[^']*', '--box', 'joppa'\]/);
  // agy spells it its own way, and the box is what the harness's own line lacks.
  assert.match(out, /agy \['agy', '--conversation', '0199aaaa[^']*', '--box', 'joppa'\]/);
});

test("an id this run already knows is reported before the container starts", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import herdr, report, watch
from broker.providers import agy

said = []
herdr.tell = lambda pane, provider, box, session: (said.append((pane, session)), (True, ""))[1]
watcher = watch.Watcher("demo", agy, None)
# The id typed to resume: a fact from the command line, not a guess.
watcher.announce("0199aaaa-bbbb-cccc-dddd-eeeeffff0000")
watcher.announce("0199aaaa-bbbb-cccc-dddd-eeeeffff0000")
print("SAID", said)
`, box(dir));
  // Once, not twice: the hook reports the same id again on its first bus call.
  assert.match(out, /SAID \[\('wA:p1', '0199aaaa-bbbb-cccc-dddd-eeeeffff0000'\)\]/);
});

test("a report renamed into the directory while the box runs is picked up", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import json, os, time
from broker.box import herdr, report, watch
from broker.providers import agy

watch.POLL = 0.02
said = []
herdr.tell = lambda pane, provider, box, session: (said.append(session), (True, ""))[1]
report.flags("demo")
watcher = watch.Watcher("demo", agy, None).start()

here = report.directory("demo")
# Written beside and renamed over, exactly as the hook does it.
temporary = os.path.join(here, ".tmp")
open(temporary, "w").write('{"agent":"antigravity","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}')
os.replace(temporary, os.path.join(here, report.NAME))
for _ in range(200):
    if said:
        break
    time.sleep(0.02)
watcher.stop()
print("SAID", said)
`, box(dir));
  assert.match(out, /SAID \['0199aaaa-bbbb-cccc-dddd-eeeeffff0000'\]/);
});

test("a refused report is said out loud and reported to nobody", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os, sys, time
sys.stderr = sys.stdout  # out.py sends every message to the operator to stderr
from broker.box import herdr, report, watch
from broker.providers import claude

watch.POLL = 0.02
said = []
herdr.tell = lambda pane, provider, box, session: (said.append(session), (True, ""))[1]
report.flags("demo")
# This run was given its id out here; the box names a different chat.
watcher = watch.Watcher("demo", claude, "0199ffff-0000-1111-2222-333344445555").start()
here = report.directory("demo")
temporary = os.path.join(here, ".tmp")
open(temporary, "w").write('{"agent":"claude","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}')
os.replace(temporary, os.path.join(here, report.NAME))
for _ in range(100):
    if watcher.refused:
        break
    time.sleep(0.02)
watcher.stop()
print("SAID", said)
`, box(dir));
  assert.match(out, /SAID \[\]/);
  assert.match(out, /was refused: the id is not the one this run was given/);
  // And the operator is told the box will not wake, rather than left guessing.
  assert.match(out, /never reported its session/);
});

test("a resumed box reports the id from its own command line, before any bus call", async (t) => {
  const dir = await temp(t);
  // The hook only fires when the agent uses the bus. After a cold restart of
  // the manager the pane is relaunched with the id right there in the
  // arguments, and that is the moment the answer is needed — so the launcher
  // reads it from argv rather than waiting, and never from a file time.
  const out = engine(`
from broker.box import herdr, report, watch
from broker.box.sessions import _session_from_argv
from broker.providers import agy

said = []
herdr.tell = lambda pane, provider, box, session: (said.append(session), (True, ""))[1]
argv = ["--conversation", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"]
watcher = watch.Watcher("joppa", agy, None)
watcher.announce(None or _session_from_argv(agy, argv))
print("SAID", said)
`, box(dir));
  assert.match(out, /SAID \['0199aaaa-bbbb-cccc-dddd-eeeeffff0000'\]/);
});

test("the same id written again is not reported again", async (t) => {
  const dir = await temp(t);
  // The hook rewrites its file on every bus call, almost always with the id it
  // wrote last time. The manager hears about an id once.
  const out = engine(`
import os, time
from broker.box import herdr, report, watch
from broker.providers import agy

watch.POLL = 0.02
said = []
herdr.tell = lambda pane, provider, box, session: (said.append(session), (True, ""))[1]
report.flags("demo")
watcher = watch.Watcher("demo", agy, None).start()
here = report.directory("demo")
for _ in range(4):
    temporary = os.path.join(here, ".tmp")
    open(temporary, "w").write('{"agent":"antigravity","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}')
    os.replace(temporary, os.path.join(here, report.NAME))
    time.sleep(0.1)
watcher.stop()
print("SAID", said)
`, box(dir));
  assert.match(out, /SAID \['0199aaaa-bbbb-cccc-dddd-eeeeffff0000'\]/);
});

test("a chat replaced in the same box is reported as the new one", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os, time
from broker.box import herdr, report, watch
from broker.providers import agy

watch.POLL = 0.02
said = []
herdr.tell = lambda pane, provider, box, session: (said.append(session), (True, ""))[1]
report.flags("demo")
watcher = watch.Watcher("demo", agy, None).start()
here = report.directory("demo")
for which in ("1111", "2222"):
    temporary = os.path.join(here, ".tmp")
    open(temporary, "w").write('{"agent":"antigravity","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff%s"}' % which)
    os.replace(temporary, os.path.join(here, report.NAME))
    time.sleep(0.15)
watcher.stop()
print("SAID", said)
`, box(dir));
  // /clear starts another conversation in the same pane and the same box.
  assert.match(out, /SAID \['0199aaaa-bbbb-cccc-dddd-eeeeffff1111', '0199aaaa-bbbb-cccc-dddd-eeeeffff2222'\]/);
});

test("claude is only held to a pinned id when there IS one", async (t) => {
  const dir = await temp(t);
  // The launcher chooses the id for a FRESH claude chat, and then a report has
  // to match it. Resuming through claude's own picker chooses inside the
  // harness, so nothing out here ever learns which chat it was — and the report
  // is then accepted on its shape, like every self-naming harness. Said out
  // loud because the module used to claim claude could never name another chat.
  const out = engine(`
from broker.box import report, watch
from broker.box.sessions import _pin_session
from broker.providers import claude

for argv in (["--resume"], ["-c"], ["--continue"], []):
    pinned, _ = _pin_session(claude, list(argv))
    kind = "picker" if argv and pinned is None else ("fresh" if pinned else "none")
    said = '{"agent":"claude","id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}'
    print(argv, kind, watch.accept(said, claude, pinned)[0] is not None)
`, box(dir));

  // A picker: no pinned id, so a stranger's chat of the same harness is taken
  // on shape alone. This is the residual risk, and it is not hidden.
  assert.match(out, /\['--resume'\] picker True/);
  assert.match(out, /\['-c'\] picker True/);
  assert.match(out, /\['--continue'\] picker True/);
  // A fresh run: the id was chosen out here, so only that id is believed.
  assert.match(out, /\[\] fresh False/);
});
