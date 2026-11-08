// A codex thread must be readable from every window, not only from the window
// that opened it. Each window has its own databases because the harness cannot
// share one, so the broker shares the threads instead.
const test = require("node:test");
const assert = require("node:assert/strict");
const { temp, engine } = require("./helpers");

// The three tables that hold a conversation, cut down to the columns these
// cases turn on. `extra` is how a later migration differs from an earlier one.
function build(version, extra) {
  return `
import sqlite3
def make(path, version, extra, threads, items):
    db = sqlite3.connect(path + "/state_5.sqlite")
    db.execute("CREATE TABLE _sqlx_migrations (version INTEGER)")
    db.execute("INSERT INTO _sqlx_migrations VALUES (%d)" % version)
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT%s)" % extra)
    for thread in threads:
        db.execute("INSERT INTO threads (id, name) VALUES (?, ?)", (thread, thread))
    db.commit(); db.close()
    db = sqlite3.connect(path + "/thread_history_1.sqlite")
    db.execute("CREATE TABLE _sqlx_migrations (version INTEGER)")
    db.execute("INSERT INTO _sqlx_migrations VALUES (%d)" % version)
    db.execute("CREATE TABLE thread_items (thread_id TEXT, item_id TEXT, "
               "PRIMARY KEY (thread_id, item_id))")
    db.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT)")
    for thread, item in items:
        db.execute("INSERT INTO thread_items VALUES (?, ?)", (thread, item))
    db.commit(); db.close()

def threads(path):
    db = sqlite3.connect(path + "/state_5.sqlite")
    try:
        return sorted(row[0] for row in db.execute("SELECT id FROM threads"))
    finally:
        db.close()

def items(path, thread):
    db = sqlite3.connect(path + "/thread_history_1.sqlite")
    try:
        return [row[0] for row in db.execute(
            "SELECT item_id FROM thread_items WHERE thread_id = ?", (thread,))]
    finally:
        db.close()
`;
}

test("a thread one window opened reaches the window next to it", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os
${build()}
from broker.providers import codex_db
own, shared = "${dir}/own", "${dir}/shared"
os.makedirs(own); os.makedirs(shared)
make(own, 59, "", ["mine"], [("mine", "a"), ("mine", "b")])
make(shared, 59, "", ["theirs"], [("theirs", "c")])
codex_db._share_threads(own, shared)
print("shared holds", threads(shared))
print("own holds", threads(own))
print("the body came too", items(shared, "mine"))
`);

  // Measured before this existed: nine window copies, each with threads the
  // other eight could not resume.
  assert.match(out, /shared holds \['mine', 'theirs'\]/);
  assert.match(out, /own holds \['mine', 'theirs'\]/);
  assert.match(out, /the body came too \['a', 'b'\]/);
});

test("a window on a later migration hands its own file over", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os, sqlite3
${build()}
from broker.providers import codex_db
own, shared = "${dir}/own", "${dir}/shared"
os.makedirs(own); os.makedirs(shared)
make(own, 62, ", kept TEXT", ["mine"], [("mine", "a")])
make(shared, 55, "", ["theirs"], [("theirs", "c")])
codex_db._share_threads(own, shared)
db = sqlite3.connect(shared + "/state_5.sqlite")
print("shared migration", db.execute("SELECT max(version) FROM _sqlx_migrations").fetchone()[0])
print("shared columns", [row[1] for row in db.execute("PRAGMA table_info(threads)")])
db.close()
print("shared holds", threads(shared))
`);

  // The harness does not open the shared set, so nothing migrates it: here it
  // sat four migrations behind and could never learn a new thread again.
  assert.match(out, /shared migration 62/);
  assert.match(out, /shared columns \['id', 'name', 'kept'\]/);
  // Nothing is lost by the handover: the shared threads were merged down first.
  assert.match(out, /shared holds \['mine', 'theirs'\]/);
});

test("an empty file left behind is not written to", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os, sqlite3
${build()}
from broker.providers import codex_db
own, shared = "${dir}/own", "${dir}/shared"
os.makedirs(own); os.makedirs(shared)
make(own, 59, "", ["mine"], [("mine", "a")])
for name in ("state_5.sqlite", "thread_history_1.sqlite"):
    sqlite3.connect(shared + "/" + name).close()  # 4096 bytes, not one table
codex_db._share_threads(own, shared)
print("size", os.path.getsize(shared + "/state_5.sqlite"))
print("own is untouched", threads(own))
`);

  // Five of these sat in one account's profile on this machine. A merge into
  // one writes rows that no reader is ever going to look for.
  assert.match(out, /size 0\b|size 4096/);
  assert.match(out, /own is untouched \['mine'\]/);
});

test("the first window makes the meeting point when there is none", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os
${build()}
from broker.providers import codex_db
own, shared = "${dir}/own", "${dir}/shared"
os.makedirs(own); os.makedirs(shared)
make(own, 61, "", ["mine"], [("mine", "a")])
codex_db._share_threads(own, shared)
print("shared index exists", os.path.isfile(shared + "/state_5.sqlite"))
print("shared holds", threads(shared))
print("the body came too", items(shared, "mine"))
`);

  // Measured on the second host here: nine window copies and not one database
  // in the shared home, so no thread was ever readable from a second window.
  assert.match(out, /shared index exists True/);
  assert.match(out, /shared holds \['mine'\]/);
  assert.match(out, /the body came too \['a'\]/);
});

test("an empty leftover does not become the meeting point", async (t) => {
  const dir = await temp(t);
  const out = engine(`
import os, sqlite3
${build()}
from broker.providers import codex_db
own, shared = "${dir}/own", "${dir}/shared"
os.makedirs(own); os.makedirs(shared)
for name in ("state_5.sqlite", "thread_history_1.sqlite"):
    sqlite3.connect(own + "/" + name).close()  # no migration table at all
codex_db._share_threads(own, shared)
print("shared index exists", os.path.isfile(shared + "/state_5.sqlite"))
`);

  // Handing that over would give every later window an empty start.
  assert.match(out, /shared index exists False/);
});
