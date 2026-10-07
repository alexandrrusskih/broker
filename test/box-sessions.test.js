// Which session a run is on, and what the box says about coming back to it.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { temp, engine } = require("./helpers");

test("the way back names the session the harness itself named", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project, { recursive: true });

  const out = engine(`
from broker import box
from broker.box import sessions, store
from broker.providers import codex
print(store._resume_hint(codex, "demo", "${project}", {}, 0, None,
                       "019efe7b-889a-72d3-8a7c-bfae7be3dacd"))
`, { HOME: dir });

  // Its own verb, and the id it printed — the line differs from the harness's
  // own only by the box on the end, so that suffix can be typed onto it.
  assert.match(out, /codex resume 019efe7b-889a-72d3-8a7c-bfae7be3dacd --box demo/);
});

test("a run that named no session says so, rather than inventing one", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const sessions = path.join(dir, ".claude", "projects", project.replace(/\//g, "-"));
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(sessions, { recursive: true });
  // Somebody else's conversation, sitting in the same directory and newer than
  // this run — which is the ordinary case: every window on a machine files its
  // sessions here. This used to be picked up and printed as yours.
  await fs.writeFile(path.join(sessions, "a-stranger.jsonl"), "{}\n");

  const out = engine(`
from broker import box
from broker.box import sessions, store
from broker.providers import claude
claude.SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"
print(store._resume_hint(claude, "demo", "${project}"))
`, { HOME: dir });

  assert.match(out, /did not name its session/);
  assert.match(out, /claude --resume --box demo/);
  assert.ok(!/a-stranger/.test(out), "no id is invented from whatever file is newest");
});

test("resuming and typing nothing still gets you back to the same session", async (t) => {
  const dir = await temp(t);
  const out = engine(`
from broker.box import sessions, store
from broker.providers import codex, claude
# The harness names no session on its way out — correctly, it started none:
# nothing was typed, so there was nothing to save. The id was in the command.
print(sessions._session_from_argv(codex, ["resume", "019efe7b-889a-72d3-8a7c-bfae7be3dacd"]))
print(sessions._session_from_argv(claude, ["--resume", "019efe7b-889a-72d3-8a7c-bfae7be3dacd"]))
print(sessions._session_from_argv(codex, ["exec", "hello"]))
`, { HOME: dir });

  const [fromCodex, fromClaude, fromPlainRun] = out.trim().split("\n");
  assert.equal(fromCodex, "019efe7b-889a-72d3-8a7c-bfae7be3dacd");
  assert.equal(fromClaude, "019efe7b-889a-72d3-8a7c-bfae7be3dacd");
  assert.equal(fromPlainRun, "None", "a fresh run has no id to take from the command");
});

test("the session a box offers to resume is its own, not the newest on the machine", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const sessions = path.join(dir, ".claude", "projects", project.split(path.sep).join("-"));
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(sessions, { recursive: true });
  // Another window, open on the same project, writing its own session into the
  // one directory they share — and writing it last.
  await fs.writeFile(path.join(sessions, "aaaaaaaa-1111-2222-3333-444444444444.jsonl"), "{}\n");

  const out = engine(`
from broker import box
from broker.box import sessions, store
from broker.providers import claude
claude.SESSION_GLOB = "%(home)s/.claude/projects/%(key)s/*.jsonl"
claude.SESSION_ID_FLAG = ("--session-id", "%s")
claude.SESSION_PICKERS = ("--resume", "-r", "--continue", "-c", "--session-id")

pinned, argv = box.sessions._pin_session(claude, ["--dangerously-skip-permissions"])
print("FLAG", argv[0], argv[1] == pinned, argv[2])
print(box.store._resume_hint(claude, "demo", "${project}", {}, 0, None, pinned))
`, { HOME: dir });

  // The id is decided before the run, passed to the harness, and printed back
  // unchanged — the neighbour's newer file never enters into it.
  assert.match(out, /FLAG --session-id True --dangerously-skip-permissions/);
  const offered = out.match(/--resume ([0-9a-f-]{36})/);
  assert.ok(offered, out);
  assert.notStrictEqual(offered[1], "aaaaaaaa-1111-2222-3333-444444444444");
});

test("naming a session yourself leaves the command exactly as you typed it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project, { recursive: true });

  const out = engine(`
from broker import box
from broker.box import sessions, store
from broker.providers import claude
claude.SESSION_ID_FLAG = ("--session-id", "%s")
claude.SESSION_PICKERS = ("--resume", "-r", "--continue", "-c", "--session-id")

# Resuming by id: yours, and already known.
print("BYID", *box.sessions._pin_session(claude, ["--resume", "bbbbbbbb-1111-2222-3333-444444444444"]))
# A picker with nothing to pick from yet: the answer lives inside the harness.
print("PICKER", box.sessions._pin_session(claude, ["--continue"])[0])
# A harness that cannot be told an id keeps the old way of finding out.
claude.SESSION_ID_FLAG = None
print("UNTOLD", box.sessions._pin_session(claude, ["--print", "hi"]))
`, { HOME: dir });

  // Nothing added, nothing reordered: a session the person named is theirs.
  assert.match(out, /BYID bbbbbbbb-1111-2222-3333-444444444444 \['--resume', 'bbbbbbbb-1111-2222-3333-444444444444'\]/);
  assert.match(out, /PICKER None/);
  assert.match(out, /UNTOLD \(None, \['--print', 'hi'\]\)/);
});
