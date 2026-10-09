// The one thing a box may say about itself: which chat is in it.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { temp, engine } = require("./helpers");

// HOME decides where the broker keeps its own files, so every test here gets
// its own machine. Patching an attribute instead was tried and it leaked.
const box = (dir, extra = {}) => ({ HOME: dir, HERDR_PANE_ID: "wA:p1", ...extra });

test("the box is given one directory, writable, at the same path, and told its name", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import report, state, watch
print("\\n".join(report.flags("demo")))
print("DIR", report.directory("demo"))
`, box(dir));

  const reported = /DIR (.+)/.exec(out)[1];
  assert.match(out, new RegExp(`AGNTBUS_SESSION_REPORT_DIR=${reported}`));
  // Same path inside as out, and writable: the hook renames a file into it.
  assert.match(out, new RegExp(`type=bind,source=${reported},target=${reported}$`, "m"));
  assert.ok(!/readonly/.test(out), "the box has to be able to write its report");
  // Under the broker's own config, which no box mounts.
  assert.match(reported, /\/\.config\/broker\/box\/reports\/demo\//);
});

test("one directory per box and per window — never a shared one", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import report, state, watch
print(report.directory("one"))
print(report.directory("two"))
`, box(dir));
  const [one, two] = out.trim().split("\n");
  assert.notEqual(one, two);
  // The window is the last element, and it is never empty.
  assert.ok(path.basename(one).length > 0);

  const other = engine(`
from broker.box import report, state, watch
print(report.directory("one"))
`, box(dir, { HERDR_PANE_ID: "wB:p9" }));
  assert.notEqual(one, other.trim(), "another window is another directory");
});

test("a report left by a crashed run is gone before the next one starts", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os
from broker.box import report, state, watch
here = report.directory("demo")
os.makedirs(here, exist_ok=True)
open(os.path.join(here, report.NAME), "w").write('{"agent":"claude","id":"dead-one-from-before"}')
report.flags("demo")
print("LEFT", os.path.exists(os.path.join(here, report.NAME)))
`, box(dir));
  // An id from a conversation that has ended would point the pane at nothing.
  assert.match(out, /LEFT False/);
});

test("the pane is never read from the report, and nothing is said without one", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import json, os, time
from broker.box import report, state, watch
from broker.providers import claude

said = []
state.set_session = lambda box, session: (said.append((box, session)), (True, ""))[1]
# A box naming a pane of its own: the field is not even looked at.
watcher = watch.Watcher("demo", claude, None)
print("PANE", repr(watcher.pane))
watcher.announce("0199aaaa-bbbb-cccc-dddd-eeeeffff0000")
print("SAID", said)
`, { HOME: dir, HERDR_PANE_ID: "" });  // this launch belongs to no pane
  assert.match(out, /PANE ''/);
  assert.match(out, /SAID \[\]/, "with no pane of our own there is nobody to report to");
});

test("the directory goes when the box does", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os
from broker.box import report, state, watch
report.flags("demo")
here = report.directory("demo")
open(os.path.join(here, report.NAME), "w").write("{}")
report.release("demo")
print("GONE", not os.path.exists(here))
`, box(dir));
  assert.match(out, /GONE True/);
});

test("no box mounts the reports of another box, or the host's own state", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(path.join(dir, ".claude"), { recursive: true });
  await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({
    one: { rw: [project] }, two: { rw: [project] },
  }));

  const out = engine(`
import os
from broker.box import boxes, report, state
boxes.PATH = "${path.join(dir, "boxes.json")}"
from broker.box.run import command
from broker.providers import claude
os.chdir("${project}")
report.flags("two")
cmd = command(claude, "one", boxes.profiles()["one"], ["--version"], {})
sources = [p.split("source=")[1].split(",")[0] for p in cmd if p.startswith("type=bind,source=")]
print("OWN", report.directory("one") in sources)
print("OTHER", any(s.startswith(os.path.dirname(report.directory("two"))) for s in sources))
print("STATE", any(s.startswith(state.root()) for s in sources))
`, box(dir));

  assert.match(out, /OWN True/, "the box gets its own report directory");
  // Its parent is never mounted, so no box can read another box's reports...
  assert.match(out, /OTHER False/);
  // ...and the file that holds the pane stays out here, where a box cannot
  // reach it. A box that could name its own pane could be woken in another
  // agent's place.
  assert.match(out, /STATE False/);
});
