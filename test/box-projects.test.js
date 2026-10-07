// A project as the box sees it: symlinks resolved, and one index per real path.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { root, temp, engine } = require("./helpers");

test("an explicit path in a box resolves symlinks too, so it cannot key a second database", async (t) => {
  const dir = await temp(t);
  const physical = path.join(dir, "elsewhere", "project");
  const link = path.join(dir, "Projects", "project");
  await fs.mkdir(physical, { recursive: true });
  await fs.mkdir(path.dirname(link), { recursive: true });
  await fs.symlink(physical, link);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", env: { CBM_ALLOWED_ROOT: "/everything" } } }
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
servers = box.mcp_servers(claude)
profile = {"mcp": {"probe": {"env": {
    "CBM_ALLOWED_ROOT": "${link}",
    "CBM_CACHE_DIR": "${dir}/cache/not-created-yet",
    "CBM_LABEL": "just a string"}}}}
env = box.mcp._bridge_env("probe", servers["probe"], profile, [])
print(json.dumps([env["CBM_ALLOWED_ROOT"], env["CBM_CACHE_DIR"], env["CBM_LABEL"]]))
`, { HOME: dir });

  const [root, cache, label] = JSON.parse(out);
  assert.equal(root, physical, "writing the symlinked spelling by hand must not start a second database");
  assert.equal(cache, path.join(dir, "cache", "not-created-yet"), "a path that does not exist yet is left as written");
  assert.equal(label, "just a string", "values that are not paths are untouched");
});

test("a symlinked project comes in under both names, and keeps its existing index", async (t) => {
  const dir = await temp(t);
  const physical = path.join(dir, "elsewhere", "project");
  const link = path.join(dir, "Projects", "project");
  await fs.mkdir(physical, { recursive: true });
  await fs.mkdir(path.dirname(link), { recursive: true });
  await fs.symlink(physical, link);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", env: { CBM_ALLOWED_ROOT: "/everything" } } }
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
box.mcp._start_bridge = lambda *a: None
cmd = box.command(claude, "demo", {"rw": ["${link}"]}, [], {})
servers = box.mcp_servers(claude)
print(json.dumps({
  "cmd": cmd,
  "root": box.mcp._bridge_env("probe", servers["probe"], {}, ["${link}"])["CBM_ALLOWED_ROOT"],
}))
`, { HOME: dir });
  const { cmd, root } = JSON.parse(out);
  const line = cmd.join(" ");

  // A host-side server resolves symlinks and answers with the physical path.
  // Without the second mount the harness inside cannot open a single file it names.
  assert.ok(line.includes(`source=${link},target=${link}`), "the name you typed");
  assert.ok(line.includes(`source=${physical},target=${physical}`), "and the path it really is");
  // The database is keyed by the path given, so the symlinked spelling would
  // start a second one and reindex the project from scratch.
  assert.equal(root, physical);
});
