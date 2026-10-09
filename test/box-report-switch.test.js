// Switching the chat inside one box: /clear, and what the pane is told next.
const test = require("node:test");
const assert = require("node:assert/strict");
const { temp, engine, MANAGER } = require("./helpers");

const box = (dir, extra = {}) => ({ HOME: dir, HERDR_PANE_ID: "wA:p1", ...extra });

test("/clear in a boxed claude switches the chat, and the wake follows it", async (t) => {
  const dir = await temp(t);
  // ph found this as a blocker. The launcher chooses the id for a fresh claude
  // chat, so the first report must match it — but the person then types /clear
  // and the harness is legitimately in a chat nobody out here named. Being
  // strict there kept the pane on the first chat for ever, and a cold restart
  // reopened the wrong one.
  const out = engine(`
import json, os, time
${MANAGER}
from broker.box import herdr, report, watch
from broker.providers import claude

sock = os.path.join("${dir}", "herdr.sock")
threading.Thread(target=listen, args=(sock,), daemon=True).start()
assert ready.wait(10), "the stand-in manager never came up"
os.environ["HERDR_SOCKET_PATH"] = sock
watch.POLL = 0.02

first = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
after_clear = "0199aaaa-bbbb-cccc-dddd-eeeeffff0002"

report.flags("demo")
here = report.directory("demo")
watcher = watch.Watcher("demo", claude, first)
watcher._backoff = 0.02
watcher.start()
# The launcher says the id it chose, before anything runs.
watcher.announce(first)
print("FIRST", watcher.sent)

def say(session):
    temporary = os.path.join(here, ".tmp")
    open(temporary, "w").write(json.dumps({"agent": "claude", "id": session}))
    os.replace(temporary, os.path.join(here, report.NAME))

# ...and the hook reports the same chat on its first bus call.
say(first)
time.sleep(0.2)
# Then /clear.
say(after_clear)
for _ in range(300):
    if watcher.sent == after_clear:
        break
    time.sleep(0.02)
watcher.stop()
print("AFTER", watcher.sent)
print("HOLDS", held.get("value"))
reports = [h["params"] for h in heard if h["method"] == "pane.report_agent"]
print("IDS", [p["agent_session_id"] for p in reports])
print("WAKE", json.dumps(reports[-1]["resume_argv"]))
`, box(dir));

  assert.match(out, /FIRST 0199aaaa-bbbb-cccc-dddd-eeeeffff0001/);
  assert.match(out, /AFTER 0199aaaa-bbbb-cccc-dddd-eeeeffff0002/);
  // The pane holds the new chat, read back from the pane itself.
  assert.match(out, /HOLDS 0199aaaa-bbbb-cccc-dddd-eeeeffff0002/);
  // The first chat was reported once, the second after it — not instead of it.
  const ids = JSON.parse(/IDS (\[.*\])/.exec(out)[1].replace(/'/g, '"'));
  assert.equal(ids[0], "0199aaaa-bbbb-cccc-dddd-eeeeffff0001");
  assert.equal(ids[ids.length - 1], "0199aaaa-bbbb-cccc-dddd-eeeeffff0002");
  // And a cold restart would reopen the chat the person is actually in.
  assert.deepEqual(JSON.parse(/WAKE (\[.*\])/.exec(out)[1]),
    ["claude", "--resume", "0199aaaa-bbbb-cccc-dddd-eeeeffff0002", "--box", "demo"]);
});

test("a chat switch is refused until the pinned id has landed", async (t) => {
  const dir = await temp(t);
  // The pinned id is proof of a beginning, not a lease. A box that names
  // another chat before proving it is the box that was given this one gets
  // nowhere — and so does a box whose first report never lands.
  const out = engine(`
import json, os, sys, time
sys.stderr = sys.stdout
from broker.box import herdr, report, watch
from broker.providers import claude

said = []
herdr.tell = lambda pane, provider, box, session: (said.append(session), (True, ""))[1]
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
