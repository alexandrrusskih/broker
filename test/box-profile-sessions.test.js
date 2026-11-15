// A box must not be charged for a directory it never reads. codex keeps its
// sessions in one pile per config directory, and siblings() binds that pile
// over the box's profile copy at its own path — so copying it is paid for
// twice and used once. Measured on one of these machines: 24 GB per box, five boxes,
// 120 GB, and the free space fell 143 GB in fifteen minutes.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { temp, engine } = require("./helpers");

// Everything chosen_home() asks of a provider, and nothing else.
function preamble(dir) {
  return `
import os, types
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
from broker.box import harness

provider = types.SimpleNamespace(NAME="codex", HOME_ENV="CODEX_HOME",
                                 BOX_SHARED=("sessions",))

def shadow(args):
    """The profile copy this run made, as the docker argument names it."""
    found = [a.partition("source=")[2].partition(",")[0] for a in args
             if a.startswith("type=bind,source=") and "/box/profiles/" in a]
    return found[0] if found else None

def seed(home):
    os.makedirs(home + "/sessions/2026/11/15")
    open(home + "/sessions/2026/11/15/rollout-a.jsonl", "w").write("a\\n")
    open(home + "/sessions/2026/11/15/rollout-b.jsonl", "w").write("b\\n")
    open(home + "/history.jsonl", "w").write("small\\n")
    os.makedirs(home + "/log")
    open(home + "/log/tui.log", "w").write("log\\n")
`;
}

test("a sibling home hands the box its config without the session pile", async (t) => {
  const dir = await temp(t);
  const out = engine(`
${preamble(dir)}
canonical = "${dir}/.codex"
home = "${dir}/.codex-sk"
seed(home)
args = harness.chosen_home(provider, {"CODEX_HOME": home}, "acme", None, [], canonical)
copy = shadow(args)
print("the copy was made", copy is not None)
print("sessions holds", os.listdir(copy + "/sessions"))
print("the mount point is there", os.path.isdir(copy + "/sessions"))
print("the rest came across", sorted(
    n for n in os.listdir(copy) if n != "sessions"))
print("the log came whole", os.path.exists(copy + "/log/tui.log"))
print("the real pile is untouched", sorted(
    os.listdir(home + "/sessions/2026/11/15")))
`);

  assert.match(out, /the copy was made True/);
  // The pile is skipped, and only the pile.
  assert.match(out, /sessions holds \[\]/);
  // Skipped, not absent: the bind has to land somewhere.
  assert.match(out, /the mount point is there True/);
  assert.match(out, /the rest came across \['history\.jsonl', 'log'\]/);
  assert.match(out, /the log came whole True/);
  assert.match(out, /the real pile is untouched \['rollout-a\.jsonl', 'rollout-b\.jsonl'\]/);
});

test("a home siblings() cannot see keeps its full copy", async (t) => {
  const dir = await temp(t);
  const out = engine(`
${preamble(dir)}
canonical = "${dir}/.codex"
# Not <canonical>-something, so siblings() never globs it and no bind will
# cover the copy. Skipping the pile here would take the sessions away.
home = "${dir}/elsewhere"
seed(home)
args = harness.chosen_home(provider, {"CODEX_HOME": home}, "acme", None, [], canonical)
copy = shadow(args)
print("sessions holds", sorted(os.listdir(copy + "/sessions/2026/11/15")))
`);

  assert.match(out, /sessions holds \['rollout-a\.jsonl', 'rollout-b\.jsonl'\]/);
});
