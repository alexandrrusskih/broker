// A bridged MCP server runs on the HOST, outside the box, and opens the host's
// files itself — so a box's mounts do not constrain it. codebase-memory is the
// case this was written for: its allowed root governs INDEXING, and a box with
// a root of its own could still read every project in the shared base. The
// listener is the only thing that sees each request and belongs to one box, so
// the rule holds here or nowhere.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { root, temp } = require("./helpers");

const ENGINE = path.join(root, "lib", "wrappers");

// `cat` stands in for the MCP server, as it does in the bridge's own tests:
// whatever reaches it comes straight back, so the echo IS the list of requests
// that got through.
async function listener(t, home, policy) {
  const env = { ...process.env, HOME: home, BROKER_REAL_HOME: home, PYTHONDONTWRITEBYTECODE: "1" };
  for (const name of Object.keys(env)) if (name.startsWith("HERDR_")) delete env[name];
  if (policy) env.BROKER_MCP_POLICY = JSON.stringify(policy);
  const child = spawn("python3", ["-m", "broker.mcpbridge", "serve", "probe", "--key", "k", "--", "cat"],
    { cwd: ENGINE, env, stdio: ["ignore", "ignore", "pipe"] });
  let bad = "";
  child.stderr.on("data", (d) => { bad += d; });
  t.after(() => child.kill());

  const file = path.join(home, ".config", "broker", "box", "mcp", "probe-k.json");
  for (let i = 0; i < 100; i++) {
    try { return { state: JSON.parse(await fs.readFile(file, "utf8")), child }; } catch (_e) { /* not yet */ }
    await new Promise((done) => setTimeout(done, 100));
  }
  throw new Error("the listener never wrote its state file. stderr: " + bad);
}

// Send every line, then read for a moment: what comes back is the answer to
// everything that was allowed plus the refusals written by the broker itself.
function exchange(state, lines) {
  return new Promise((resolve, reject) => {
    const conn = net.connect({ host: state.host, port: state.port, family: 4 });
    let seen = "";
    conn.on("error", reject);
    conn.on("connect", () => {
      conn.write(state.token + "\n");
      for (const line of lines) conn.write(JSON.stringify(line) + "\n");
    });
    conn.on("data", (d) => { seen += d; });
    setTimeout(() => {
      conn.destroy();
      resolve(seen.split("\n").filter(Boolean).map((l) => JSON.parse(l)));
    }, 1500);
  });
}

const call = (id, name, args) =>
  ({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } });

test("a rule decides what a box may ask a bridged server", async (t) => {
  const home = await temp(t);
  const { state } = await listener(t, home, {
    deny_tools: ["delete_project"],
    args: { project: ["Volumes-hdd-Projects-joppa", "joppa"] }
  });
  assert.equal(state.filtered, true, "the state file says this listener filters");

  const back = await exchange(state, [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: {} },
    call(2, "search_graph", { project: "joppa", query: "x" }),
    call(3, "search_graph", { project: "Volumes-hdd-Projects-ama", query: "password" }),
    call(4, "get_code_snippet", { project: "joppa-but-not-really", qualified_name: "x" }),
    call(5, "delete_project", { project: "joppa" }),
    call(6, "list_projects", {})
  ]);
  const byId = new Map(back.map((m) => [m.id, m]));

  // Through: anything that is not a tools/call, and a call whose named
  // argument matches the rule whole.
  assert.equal(byId.get(1).method, "initialize", "a request that is not a tools/call is not touched");
  assert.equal(byId.get(2).params.arguments.project, "joppa", "the box's own project goes through");
  assert.equal(byId.get(6).params.name, "list_projects", "a call with no named argument is not checked");

  // Refused: by the broker, before the server saw it.
  assert.match(byId.get(3).error.message, /project='Volumes-hdd-Projects-ama'.*outside/,
    "another project is refused, and the message says which");
  assert.match(byId.get(4).error.message, /outside/,
    "a pattern must match the WHOLE value, so a longer name is not the project");
  assert.match(byId.get(5).error.message, /'delete_project'.*not available/,
    "a denied tool is refused whatever its arguments say");
  for (const id of [3, 4, 5]) {
    assert.equal(byId.get(id).method, undefined, `request ${id} never reached the server`);
  }
});

test("without a rule the bridge carries everything, as it always did", async (t) => {
  const home = await temp(t);
  const { state } = await listener(t, home, null);
  assert.equal(state.filtered, false, "and says so");
  const back = await exchange(state, [call(1, "delete_project", { project: "anything" })]);
  assert.equal(back[0].params.name, "delete_project", "no rule, no filtering");
});
