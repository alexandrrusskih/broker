// Telling the terminal manager, and being believed. The first version of this
// sent the report and called it delivered as soon as the socket had been read
// at all, so a refusal was recorded as a success: the pane knew nothing and the
// launcher believed it had said everything.
const test = require("node:test");
const assert = require("node:assert/strict");
const { temp, engine, MANAGER } = require("./helpers");

// A stand-in for the manager, answering the way the patched one does: it takes
// a session from a source it does not already trust for that pane only through
// pane.report_agent, and refuses pane.report_agent_session with
// session_not_accepted.

test("the first report goes through the call the manager will take", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import json, os, time
${MANAGER}
from broker.box import herdr, report, watch
from broker.providers import agy

sock = os.path.join("${dir}", "herdr.sock")
threading.Thread(target=listen, args=(sock,), daemon=True).start()
assert ready.wait(10), "the stand-in manager never came up"
os.environ["HERDR_SOCKET_PATH"] = sock

taken, why = herdr.tell("wA:p1", agy, "joppa", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000")
print("TAKEN", taken, repr(why))
print("METHOD", heard[0]["method"])
print("THEN", heard[1]["method"])
print("PARAMS", json.dumps({k: heard[0]["params"][k] for k in
      ("pane_id", "source", "agent", "state", "agent_session_id", "resume_argv")}, sort_keys=True))
`, { HOME: dir });

  assert.match(out, /TAKEN True ''/);
  // report_agent, not report_agent_session: the narrower call is refused until
  // the source already holds the pane's agent, which the broker never does on a
  // first report. Proved against the patched manager by codex-misc-p5.
  assert.match(out, /METHOD pane\.report_agent$/m);
  // And then the pane is read back, because ok does not mean applied.
  assert.match(out, /THEN pane\.get/);
  const params = JSON.parse(/PARAMS (.+)/.exec(out)[1]);
  assert.equal(params.source, "broker:box");
  assert.equal(params.agent, "agy");
  assert.equal(params.state, "idle");
  assert.equal(params.pane_id, "wA:p1");
  assert.equal(params.agent_session_id, "0199aaaa-bbbb-cccc-dddd-eeeeffff0000");
  // Built here, from the box and the harness — never out of the report.
  assert.deepEqual(params.resume_argv,
    ["agy", "--conversation", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000", "--box", "joppa"]);
});

test("a refusal is a refusal, and is not remembered as a report delivered", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os, sys, time
sys.stderr = sys.stdout  # every message to the operator goes to stderr
${MANAGER}
from broker.box import herdr, report, watch
from broker.providers import agy

sock = os.path.join("${dir}", "herdr.sock")
threading.Thread(target=listen, args=(sock,), daemon=True).start()
assert ready.wait(10), "the stand-in manager never came up"
os.environ["HERDR_SOCKET_PATH"] = sock
os.environ["HERDR_PANE_ID"] = "wA:p1"

# The manager here refuses anything that is not pane.report_agent.
herdr.tell = lambda pane, provider, box, session: herdr.accepted(
    '{"error":{"message":"session_not_accepted"}}')
watcher = watch.Watcher("joppa", agy, None)
watcher.announce("0199aaaa-bbbb-cccc-dddd-eeeeffff0000")
print("SENT", repr(watcher.sent))
watcher.stop()
`, { HOME: dir });

  assert.match(out, /SENT None/, "a refused report must not count as sent");
  assert.match(out, /did not take the 'joppa' box's session: session_not_accepted/);
  // And the operator hears the conclusion, not only the detail.
  assert.match(out, /never reported its session/);
});

test("an answer that cannot be understood is not a result", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import herdr
for answer in ("", "not json", '"a string"', "{}", '{"result": null}',
               '{"error": {"message": "session_not_accepted"}}',
               '{"result": {"accepted": false, "reason": "no"}}',
               '{"result": {"type": "ok"}}'):
    result, why = herdr.accepted(answer)
    print(repr(answer), result is not None, repr(why))
`, { HOME: dir });

  // Fails closed: only a clear result is a result.
  assert.match(out, /^'' False/m);
  assert.match(out, /^'not json' False/m);
  assert.match(out, /^'"a string"' False/m);
  assert.match(out, /^'{}' False/m);
  assert.match(out, /^'{"result": null}' False/m);
  assert.match(out, /^'{"error".*False 'session_not_accepted'$/m);
  assert.match(out, /^'{"result": {"accepted": false.*False 'no'$/m);
  // The only yes: a result that is there and does not say otherwise.
  assert.match(out, /^'{"result": {"type": "ok"}}' True ''$/m);
});

test("ok is not applied: a pane another source holds is not ours", async (t) => {
  const dir = await temp(t);
  // Proved against a live socket by codex-misc-p5: a pane already held by one
  // source kept its own agent and id, and the conflicting report was still
  // answered ok. So the answer settles nothing and the pane settles everything.
  const out = engine(`
import json, os, time
${MANAGER}
from broker.box import herdr
from broker.providers import agy, claude

sock = os.path.join("${dir}", "herdr.sock")
threading.Thread(target=listen, args=(sock,), daemon=True).start()
assert ready.wait(10), "the stand-in manager never came up"
os.environ["HERDR_SOCKET_PATH"] = sock

# Somebody else got there first, and it is not the broker.
held.update(agent="claude", source="herdr:claude", value="0199bbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
print("TAKEN", herdr.tell("wA:p1", agy, "joppa", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"))
print("METHODS", [h["method"] for h in heard])
`, { HOME: dir });

  assert.match(out, /TAKEN \(False, 'it answered ok but the pane still holds herdr:claude\/claude 0199bbbb/);
  // It reported, then read the pane back. Both calls, in that order.
  assert.match(out, /METHODS \['pane\.report_agent', 'pane\.get'\]/);
});

test("no socket is not a report either", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os
os.environ.pop("HERDR_SOCKET_PATH", None)
from broker.box import herdr
from broker.providers import claude
print(herdr.tell("wA:p1", claude, "demo", "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"))
`, { HOME: dir });
  assert.match(out, /\(False, 'this pane has no manager socket'\)/);
});

test("a managed pane does not start a box it could never wake", async (t) => {
  const dir = await temp(t);
  // The report directory cannot be made: its parent is a file.
  const out = engine(`
import os, sys
sys.stderr = sys.stdout
from broker import config
from broker.box import report, watch
# The config directory is moved, as a test or another home moves it — the root
# is asked for every time, so this is all it takes.
config.CONFIG_DIR = os.path.join("${dir}", "cfg")
os.makedirs(os.path.join(config.CONFIG_DIR, "box"), exist_ok=True)
open(os.path.join(config.CONFIG_DIR, "box", "reports"), "w").write("")
try:
    report.flags("joppa")
    print("STARTED ANYWAY")
except SystemExit as exit:
    print("REFUSED", exit.code)
`, { HOME: dir, HERDR_PANE_ID: "wA:p1" });

  // Starting would mean a box that works and can never be woken, with nothing
  // on screen saying so until somebody tried.
  assert.match(out, /cannot make its report directory/);
  assert.match(out, /this pane could not wake it, so it is not started/);
  assert.match(out, /REFUSED 1/);

  const loose = engine(`
import os, sys
sys.stderr = sys.stdout
from broker import config
from broker.box import report, watch
config.CONFIG_DIR = os.path.join("${dir}", "cfg")
os.makedirs(os.path.join(config.CONFIG_DIR, "box"), exist_ok=True)
if not os.path.exists(os.path.join(config.CONFIG_DIR, "box", "reports")):
    open(os.path.join(config.CONFIG_DIR, "box", "reports"), "w").write("")
print("FLAGS", report.flags("joppa"))
`, { HOME: dir, HERDR_PANE_ID: "" });
  // Outside a pane there is nothing to wake, so it is only worth saying.
  assert.match(loose, /it will not wake by itself/);
  assert.match(loose, /FLAGS \[\]/);
});
