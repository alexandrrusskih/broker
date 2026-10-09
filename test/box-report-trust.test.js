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
from broker.box import report
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
    print(label, report._accept(raw, provider, pinned)[0])
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
from broker.box import report
from broker.providers import agy, claude, codex
for provider in (claude, codex, agy):
    print(provider.NAME, report._resume_argv(provider, "joppa", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"))
`, box(dir));
  assert.match(out, /claude \['claude', '--resume', '0199aaaa[^']*', '--box', 'joppa'\]/);
  assert.match(out, /codex \['codex', 'resume', '0199aaaa[^']*', '--box', 'joppa'\]/);
  // agy spells it its own way, and the box is what the harness's own line lacks.
  assert.match(out, /agy \['agy', '--conversation', '0199aaaa[^']*', '--box', 'joppa'\]/);
});

test("an id this run already knows is reported before the container starts", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import report
from broker.providers import agy

said = []
report._tell = lambda pane, provider, box, session: said.append((pane, session)) or True
watcher = report.Watcher("demo", agy, None)
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
from broker.box import report
from broker.providers import agy

report.POLL = 0.02
said = []
report._tell = lambda pane, provider, box, session: said.append(session) or True
report.flags("demo")
watcher = report.Watcher("demo", agy, None).start()

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
from broker.box import report
from broker.providers import claude

report.POLL = 0.02
said = []
report._tell = lambda pane, provider, box, session: said.append(session) or True
report.flags("demo")
# This run was given its id out here; the box names a different chat.
watcher = report.Watcher("demo", claude, "0199ffff-0000-1111-2222-333344445555").start()
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
from broker.box import report
from broker.box.sessions import _session_from_argv
from broker.providers import agy

said = []
report._tell = lambda pane, provider, box, session: said.append(session) or True
argv = ["--conversation", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"]
watcher = report.Watcher("joppa", agy, None)
watcher.announce(None or _session_from_argv(agy, argv))
print("SAID", said)
`, box(dir));
  assert.match(out, /SAID \['0199aaaa-bbbb-cccc-dddd-eeeeffff0000'\]/);
});
