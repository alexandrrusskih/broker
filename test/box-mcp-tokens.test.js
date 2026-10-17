// A login should not depend on WHERE it was made. agy's MCP token store is
// cloned per box, because the harness rewrites that file whole and a shared one
// let one box empty everybody's logins. The cost used to be both directions: a
// login made in a box died with the box, and a login made on the host reached a
// running box only after it was restarted. These two carry it both ways.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { root, temp } = require("./helpers");

// The engine, with its config dir pointed somewhere this test owns.
function engine(home, code) {
  const body = "import sys, os, json;" +
    `sys.path.insert(0, ${JSON.stringify(path.join(root, "lib", "wrappers"))});` +
    "from broker import config;" +
    "config.CONFIG_DIR = os.environ['BROKER_TEST_CONFIG'];" +
    "from broker.box import clones;" + code;
  return execFileSync("python3", ["-c", body], {
    encoding: "utf8",
    env: { ...process.env, HOME: home, BROKER_TEST_CONFIG: path.join(home, "cfg"),
           PYTHONDONTWRITEBYTECODE: "1" }
  });
}

const entry = (expiry) => ({ token: { access_token: "a", expiry } });

// Two servers whose logins MAY be held in more than one place. Which ones
// those are is declared by the BOX, as shareLogins; the tests pass that list
// in. The last test covers a server the box did not name.
const one = "https://ntk.otion.us/mcp-claude";
const two = "https://ntk.otion.us/mcp-agy";

async function store(file, entries) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, JSON.stringify(entries));
}

test("a login made inside a box reaches the host, and the host's wins when it is newer", async (t) => {
  const home = await temp(t);
  const host = path.join(home, "mcp_oauth_tokens.json");
  const clone = (box) => path.join(home, "cfg", "box", "private", box, "agy",
    "mcp_oauth_tokens.json");

  await store(host, { [one]: entry("2026-10-16T20:00:00Z") });
  // One box logged into a server the host never had, and refreshed another
  // LATER than the host holds it.
  await store(clone("joppa"), {
    [two]: entry("2026-10-17T10:00:00Z"),
    [one]: entry("2026-10-16T23:00:00Z")
  });
  // A second box holds an OLDER ntk, which must not be pushed backwards.
  await store(clone("m"), { [one]: entry("2026-10-15T01:00:00Z") });

  engine(home, `clones._carry_back(${JSON.stringify(host)}, 'agy', ['https://ntk.otion.us/'])`);

  const merged = JSON.parse(await fs.readFile(host, "utf8"));
  assert.deepEqual(Object.keys(merged).sort(), [one, two].sort(),
    "a server only a box knew about is now the host's too");
  assert.equal(merged[one].token.expiry, "2026-10-16T23:00:00Z",
    "of two entries for one server, the later expiry is kept");
});

test("a box already running sees a new login without being restarted", async (t) => {
  const home = await temp(t);
  const host = path.join(home, "mcp_oauth_tokens.json");
  const running = path.join(home, "cfg", "box", "private", "m", "agy",
    "mcp_oauth_tokens.json");

  await store(host, { [one]: entry("2026-10-16T20:00:00Z"), [two]: entry("2026-10-17T10:00:00Z") });
  await store(running, { [one]: entry("2026-10-16T20:00:00Z") });
  // The inode is the whole point: a bind mount holds the one it was given, so
  // replacing the file would leave the running box bound to the old one and
  // blind to this.
  const before = (await fs.stat(running)).ino;

  engine(home, `clones._mirror_out(${JSON.stringify(host)}, 'agy', None, ['https://ntk.otion.us/'])`);

  const seen = JSON.parse(await fs.readFile(running, "utf8"));
  assert.deepEqual(Object.keys(seen).sort(), [one, two].sort(), "the new login arrived");
  assert.equal((await fs.stat(running)).ino, before,
    "written in place, so the box's mount still points at this file");
});

test("neither half touches anything that is not a store of logins", async (t) => {
  const home = await temp(t);
  // A database is the other kind of private file, and some are gigabytes.
  const db = path.join(home, "state.sqlite");
  await fs.writeFile(db, Buffer.concat([Buffer.from("SQLite format 3\0"), Buffer.alloc(200)]));
  const list = path.join(home, "list.json");
  await fs.writeFile(list, JSON.stringify(["not", "entries"]));
  const flat = path.join(home, "flat.json");
  await fs.writeFile(flat, JSON.stringify({ server: "a string, not an entry" }));

  for (const file of [db, list, flat]) {
    const said = engine(home, `print(clones._entries(${JSON.stringify(file)}) is None)`);
    assert.equal(said.trim(), "True", `${path.basename(file)} is not treated as a store`);
  }
  // And a run over them changes nothing.
  const was = await fs.readFile(db);
  engine(home, `clones._carry_back(${JSON.stringify(db)}, 'agy', ['https://ntk.otion.us/'])`);
  assert.deepEqual(await fs.readFile(db), was, "the database is byte-for-byte untouched");
});

// A refresh token that is single-use cannot be copied: the second holder
// presents one the server has already consumed, and a server that detects
// that revokes the whole family. So sharing is an allowlist the box declares,
// and a server it does not name keeps its login where it was made.
test("a login that cannot be held twice is left where it was made", async (t) => {
  const home = await temp(t);
  const host = path.join(home, "mcp_oauth_tokens.json");
  const clone = path.join(home, "cfg", "box", "private", "joppa", "agy",
    "mcp_oauth_tokens.json");
  const ntk = "https://ntk.otion.us/mcp-claude";
  const joppa = "https://joppa.otion.us/mcp";

  await store(host, { [ntk]: entry("2026-10-16T20:00:00Z"), [joppa]: entry("2026-10-16T20:00:00Z") });
  await store(clone, { [ntk]: entry("2026-10-16T23:00:00Z"), [joppa]: entry("2026-10-17T10:00:00Z") });

  engine(home, `clones._carry_back(${JSON.stringify(host)}, 'agy', ['https://ntk.otion.us/'])`);
  const after = JSON.parse(await fs.readFile(host, "utf8"));
  assert.equal(after[ntk].token.expiry, "2026-10-16T23:00:00Z", "ntk travels");
  assert.equal(after[joppa].token.expiry, "2026-10-16T20:00:00Z",
    "joppa does not: a second holder would revoke the family");

  engine(home, `clones._mirror_out(${JSON.stringify(host)}, 'agy', None, ['https://ntk.otion.us/'])`);
  const inBox = JSON.parse(await fs.readFile(clone, "utf8"));
  assert.equal(inBox[joppa].token.expiry, "2026-10-17T10:00:00Z",
    "and the box keeps the joppa login it made itself, rather than losing it to the host's");
});
