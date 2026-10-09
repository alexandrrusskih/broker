// Switching the chat inside one box: /clear, and what the host state file says next.
const test = require("node:test");
const assert = require("node:assert/strict");
const { temp, engine } = require("./helpers");

const box = (dir, extra = {}) => ({ HOME: dir, HERDR_PANE_ID: "wA:p1", ...extra });

test("/clear in a boxed claude switches the chat in the host state file", async (t) => {
  const dir = await temp(t);
  // ph found this as a blocker. The launcher chooses the id for a fresh claude
  // chat, so the first report must match it — but the person then types /clear
  // and the harness is legitimately in a chat nobody out here named. The state
  // file is what agntbus reads to wake the pane, so it must follow the switch,
  // and the old chat must stop matching.
  const out = engine(`
import json, os, time
from broker.box import report, state, watch
from broker.providers import claude

watch.POLL = 0.02
first = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
after_clear = "0199aaaa-bbbb-cccc-dddd-eeeeffff0002"

state.claim("demo", claude, first)
report.flags("demo")
here = report.directory("demo")
watcher = watch.Watcher("demo", claude, first)
watcher._backoff = 0.02
watcher.start()
watcher.announce(first)
print("FIRST", watcher.sent)

def say(session):
    temporary = os.path.join(here, ".tmp")
    open(temporary, "w").write(json.dumps({"agent": "claude", "id": session}))
    os.replace(temporary, os.path.join(here, report.NAME))

say(first)
time.sleep(0.2)
say(after_clear)
for _ in range(300):
    if watcher.sent == after_clear:
        break
    time.sleep(0.02)
watcher.stop()
entry = json.load(open(state.path("demo")))
print("HOLDS", entry["session"], entry["confirmed_by"])
print("PANE", entry["pane"], entry["pid"] == os.getpid())
print("MODE", oct(os.stat(state.path("demo")).st_mode & 0o777))
`, box(dir));

  assert.match(out, /FIRST 0199aaaa-bbbb-cccc-dddd-eeeeffff0001/);
  assert.match(out, /HOLDS 0199aaaa-bbbb-cccc-dddd-eeeeffff0002 report/);
  // The pane is the launcher's own, and the pid lets a reader see it lives.
  assert.match(out, /PANE wA:p1 True/);
  assert.match(out, /MODE 0o600/);
});

test("a /clear refused before the pin landed is read again once it lands", async (t) => {
  const dir = await temp(t);
  // The file is rewritten only on the next bus call. A switch refused while
  // the pin was outstanding must not be forgotten when the pin then lands.
  const out = engine(`
import json, os, time
from broker.box import report, state, watch
from broker.providers import claude

watch.POLL = 0.02
first = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
after_clear = "0199aaaa-bbbb-cccc-dddd-eeeeffff0002"
state.claim("demo", claude, first)
report.flags("demo")
here = report.directory("demo")
real = state.set_session
down = [True]
state.set_session = lambda box, session: (False, "down") if down[0] else real(box, session)
watcher = watch.Watcher("demo", claude, first)
watcher._backoff = 0.02
watcher.start()
watcher.announce(first)
temporary = os.path.join(here, ".tmp")
open(temporary, "w").write(json.dumps({"agent": "claude", "id": after_clear}))
os.replace(temporary, os.path.join(here, report.NAME))
time.sleep(0.3)
down[0] = False
for _ in range(300):
    if watcher.sent == after_clear:
        break
    time.sleep(0.02)
watcher.stop()
print("HOLDS", json.load(open(state.path("demo")))["session"])
`, box(dir));
  assert.match(out, /HOLDS 0199aaaa-bbbb-cccc-dddd-eeeeffff0002/);
});

test("a launch never rewrites or removes another launch's state file", async (t) => {
  const dir = await temp(t);
  // Same pane, same path: a box started again while the old launcher is still
  // in its finally. The pid in the file decides whose it is.
  const out = engine(`
import json, os
from broker import config
from broker.box import state
from broker.providers import claude
state.claim("demo", claude, "0199aaaa-bbbb-cccc-dddd-eeeeffff0001")
entry = json.load(open(state.path("demo")))
entry["pid"] = entry["pid"] + 100000
config.write_json(state.path("demo"), entry)
print("SET", state.set_session("demo", "0199aaaa-bbbb-cccc-dddd-eeeeffff0002"))
state.release("demo")
print("KEPT", json.load(open(state.path("demo")))["session"])
`, box(dir));
  assert.match(out, /SET \(False, 'this launch has no state file'\)/);
  assert.match(out, /KEPT 0199aaaa-bbbb-cccc-dddd-eeeeffff0001/);
});

test("a chat switch is refused until the pinned id has landed", async (t) => {
  const dir = await temp(t);
  // The pinned id is proof of a beginning, not a lease. A box that names
  // another chat before proving it is the box that was given this one gets
  // nowhere — and so does a box whose first report never lands.
  const out = engine(`
import json, os, sys, time
sys.stderr = sys.stdout
from broker.box import report, state, watch
from broker.providers import claude

said = []
state.set_session = lambda box, session: (said.append(session), (True, ""))[1]
watch.POLL = 0.02
report.flags("demo")
here = report.directory("demo")
watcher = watch.Watcher("demo", claude, "0199aaaa-bbbb-cccc-dddd-eeeeffff0001")
watcher._backoff = 0.02
watcher.start()

def say(session):
    temporary = os.path.join(here, ".tmp")
    open(temporary, "w").write(json.dumps({"agent": "claude", "id": session}))
    os.replace(temporary, os.path.join(here, report.NAME))

# A stranger's chat, first thing, with nothing proved.
say("0199ffff-0000-1111-2222-333344445555")
time.sleep(0.3)
print("STOLEN", said)
# Now the chat this run was actually given, and then a switch.
say("0199aaaa-bbbb-cccc-dddd-eeeeffff0001")
for _ in range(200):
    if said:
        break
    time.sleep(0.02)
say("0199aaaa-bbbb-cccc-dddd-eeeeffff0002")
for _ in range(300):
    if len(said) > 1:
        break
    time.sleep(0.02)
watcher.stop()
print("THEN", said)
`, box(dir));

  assert.match(out, /STOLEN \[\]/, "nothing is reported before the beginning is proved");
  assert.match(out, /was refused: the id is not the one this run was given/);
  assert.match(out, /THEN \['0199aaaa-bbbb-cccc-dddd-eeeeffff0001', '0199aaaa-bbbb-cccc-dddd-eeeeffff0002'\]/);
});
