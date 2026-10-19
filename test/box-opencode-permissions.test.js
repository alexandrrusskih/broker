// claude, agy and codex each take a flag that turns their permission prompts
// off inside a box. opencode has no such flag — not at the top level, not on
// `run` — so it stopped on every write and every shell command, in a container
// where nobody can answer the prompt. Its only lever is the config, and the
// config arrives in a variable that LAYERS over the real one.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { temp, engine } = require("./helpers");

// Every -e the command line carries, as a map.
function environment(cmd) {
  const found = new Map();
  cmd.forEach((part, i) => {
    if (cmd[i - 1] === "-e") {
      const at = part.indexOf("=");
      found.set(part.slice(0, at), part.slice(at + 1));  // docker takes the last
    }
  });
  return found;
}

async function opencodeHome(dir) {
  await fs.mkdir(path.join(dir, ".local", "share", "opencode"), { recursive: true });
  await fs.writeFile(path.join(dir, ".local", "share", "opencode", "opencode.db"), "x");
  await fs.mkdir(path.join(dir, "project"), { recursive: true });
}

test("opencode is told it may act, through the only lever it has", async (t) => {
  const dir = await temp(t);
  await opencodeHome(dir);

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import opencode
opencode.MCP_CONFIG = None
print(json.dumps(box.command(opencode, "demo", {"rw": ["${path.join(dir, "project")}"]},
                             ["run", "hi"], {})))
`, { HOME: dir });
  const env = environment(JSON.parse(out));

  const carried = env.get("OPENCODE_CONFIG_CONTENT");
  assert.ok(carried, "without this the harness asks permission and nobody is there to give it");
  const config = JSON.parse(carried);
  assert.equal(config.permission, "allow");
  // It must stay a LAYER. The host's own config holds the MCP servers and the
  // providers, and a box that replaced it instead of adding to it would lose
  // both — so nothing else belongs in here.
  assert.deepEqual(Object.keys(config).sort(), ["$schema", "permission"],
    "anything more would start replacing the real config rather than layering on it");
});

test("the box's own env still wins, so a narrower box is possible", async (t) => {
  const dir = await temp(t);
  await opencodeHome(dir);

  // Docker takes the LAST -e for a repeated name, and the box's own settings
  // are documented to win over everything above them. Someone who wants
  // opencode to keep asking inside a box must be able to say so.
  const mine = JSON.stringify({ permission: { bash: "deny" } });
  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import opencode
opencode.MCP_CONFIG = None
print(json.dumps(box.command(opencode, "demo",
                             {"rw": ["${path.join(dir, "project")}"],
                              "env": {"OPENCODE_CONFIG_CONTENT": ${JSON.stringify(mine)}}},
                             ["run", "hi"], {})))
`, { HOME: dir });

  assert.equal(environment(JSON.parse(out)).get("OPENCODE_CONFIG_CONTENT"), mine,
    "the box's own word is the last one");
});

test("a harness that has a flag is not given a variable instead", async (t) => {
  const dir = await temp(t);
  await fs.mkdir(path.join(dir, "project"), { recursive: true });

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(claude, "demo", {"rw": ["${path.join(dir, "project")}"]}, ["-p", "hi"],
                  {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
print(json.dumps(cmd))
`, { HOME: dir, BROKER_REAL_HOME: dir });
  const cmd = JSON.parse(out);

  assert.ok(cmd.includes("--dangerously-skip-permissions"), "claude still gets its flag");
  assert.equal(environment(cmd).get("OPENCODE_CONFIG_CONTENT"), undefined,
    "and nothing of another harness's leaks into it");
});
