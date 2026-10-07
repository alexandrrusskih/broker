// Which MCP servers a box gets: the ones that must be bridged, the ones it can
// dial itself, and the file the run was actually pointed at.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const net = require("node:net");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const root = path.join(__dirname, "..");

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-mcp-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  // realpath: on macOS the temp directory sits under /var, which is itself a
  // symlink to /private/var — and the code under test resolves symlinks.
  return fs.realpath(dir);
}

function engine(code, env = {}) {
  return execFileSync("python3", ["-c", `import sys; sys.path.insert(0, 'lib/wrappers')\n${code}`],
    { cwd: root, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", ...env } });
}

test("only command-started MCP servers are bridged; http ones are left alone", async (t) => {
  const dir = await temp(t);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: {
      local: { command: "/opt/tool/mcp", args: ["serve"], env: { TOOL_ROOT: "/somewhere" } },
      remote: { type: "http", url: "https://example.test/mcp" }
    }
  }));
  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
print(json.dumps(box.mcp_servers(claude)))
`);
  assert.deepEqual(JSON.parse(out), {
    // A server reached over the network needs nothing from us.
    local: { command: ["/opt/tool/mcp", "serve"], env: { TOOL_ROOT: "/somewhere" }, inherit: [] }
  });
});

test("the shim stands in for the server's own command, at its own path", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", args: ["serve"] } }
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
box.mcp._start_bridge = lambda *a: {"port": 41234, "token": "fake-secret", "command": ["x"]}
print(json.dumps(box.command(claude, "demo", {"rw": ["${project}"]}, [], {})))
`, { HOME: dir });

  const cmd = JSON.parse(out);
  const line = cmd.join(" ");
  // The harness config already points at /opt/tool/mcp — so that is where the
  // stand-in goes, and nothing has to be rewritten.
  // One file per window: writing this replaces the inode, and a bind mount
  // holds the inode it was given — a shared name meant a box starting up
  // pulled the shim out from under every box already running.
  const mount = line.match(/source=(\S*box\/shims\/claude\/probe-[^,]+),target=\/opt\/tool\/mcp,readonly/);
  assert.ok(mount, line);
  assert.ok(line.includes("--add-host host.docker.internal:host-gateway"));

  const shim = await fs.readFile(mount[1], "utf8");
  assert.match(shim, /HOST, PORT, TOKEN = 'host\.docker\.internal', 41234, 'fake-secret'/);
  assert.match(shim, /read1/, "read() on a pipe blocks for a full buffer and would deadlock the bridge");
  assert.equal((await fs.stat(mount[1])).mode & 0o777, 0o700);
});

test("a box can turn bridging off, and can say what a server should see", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  await fs.writeFile(path.join(dir, ".claude.json"), JSON.stringify({
    mcpServers: { probe: { command: "/opt/tool/mcp", env: { CBM_ALLOWED_ROOT: "/everything" } } }
  }));

  const off = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
print(json.dumps(box.command(claude, "demo", {"rw": ["${project}"], "mcp": False}, [], {})))
`, { HOME: dir });
  assert.ok(!off.includes("/opt/tool/mcp"), "'mcp': false means no bridges at all");

  // A server that answers questions about code must answer about THIS box's code.
  const env = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import claude
claude.MCP_CONFIG = ("${dir}/.claude.json", "json", "mcpServers")
servers = box.mcp_servers(claude)
print(json.dumps([
  box.mcp._bridge_env("probe", servers["probe"], {}, ["${project}"])["CBM_ALLOWED_ROOT"],
  box.mcp._bridge_env("probe", servers["probe"], {"mcp": {"probe": {"env": {"CBM_ALLOWED_ROOT": "/explicit"}}}}, ["${project}"])["CBM_ALLOWED_ROOT"],
]))
`, { HOME: dir });
  assert.deepEqual(JSON.parse(env), [project, "/explicit"]);
});

test("MCP is read from the profile the run actually uses, not the canonical home", async (t) => {
  const dir = await temp(t);
  const canonical = path.join(dir, ".codex");
  const profile = path.join(dir, ".codex-sk");
  await fs.mkdir(canonical);
  await fs.mkdir(profile);
  // The same server, spelled differently in each — which is what actually
  // happened: one through /Volumes, the other through ~/Projects.
  await fs.writeFile(path.join(canonical, "config.toml"),
    '[mcp_servers.probe]\ncommand = "/canonical/path/mcp"\n');
  await fs.writeFile(path.join(profile, "config.toml"),
    '[mcp_servers.probe]\ncommand = "/profile/path/mcp"\n');

  const read = (env) => JSON.parse(engine(`
import json
from broker import box
from broker.box import boxes, mcp, run
from broker.providers import codex
codex.MCP_CONFIG = ("${canonical}/config.toml", "toml", "mcp_servers")
codex.CANONICAL_HOME = ${JSON.stringify(canonical)}
print(json.dumps(box.mcp_servers(codex, ${JSON.stringify(env)})["probe"]["command"]))
`, { HOME: dir }));

  // An account's profile carries its own copy, and the two drift. Reading the
  // canonical one mounts the stand-in where the harness never looks, and the
  // server is reported missing.
  assert.deepEqual(read({ CODEX_HOME: profile }), ["/profile/path/mcp"]);
  assert.deepEqual(read({}), ["/canonical/path/mcp"], "with no profile in play, nothing changes");
});

test("a run that owns its MCP file bridges nothing of yours, and leaves the shared file alone", () => {
  const program = `
import json, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'lib/wrappers')
from broker import layout
from broker.box import mcp as boxmcp
from broker.providers import agy

with tempfile.TemporaryDirectory() as d:
    home = Path(d) / 'home'
    (home / '.gemini' / 'config').mkdir(parents=True)
    (home / '.gemini' / 'antigravity-cli').mkdir(parents=True)
    shared = home / '.gemini' / 'config' / 'mcp_config.json'
    yours = {"mcpServers": {"srv-a": {"command": "/bin/a"},
                            "srv-b": {"command": "/bin/b"}}}
    shared.write_text(json.dumps(yours))

    with patch.object(agy, 'CANONICAL_HOME', str(home)), \
         patch.object(agy, 'MCP_CONFIG', (str(shared), 'json', 'mcpServers')):
        plain, iso = Path(d) / 'p-plain', Path(d) / 'p-iso'
        layout.mirror(agy, str(plain))
        layout.mirror(agy, str(iso), isolate_mcp=True)

        # An ordinary profile keeps bridging what you declared.
        assert sorted(boxmcp.mcp_servers(agy, {'HOME': str(home)})) == ['srv-a', 'srv-b']
        assert sorted(boxmcp.mcp_servers(agy, {'HOME': str(plain)})) == ['srv-a', 'srv-b']

        # The isolated one bridges nothing, and needs no file to say so: the
        # profile owns the path, and an absent file is already zero servers.
        target = iso / '.gemini' / 'config' / 'mcp_config.json'
        assert not target.exists()
        assert boxmcp.mcp_servers(agy, {'HOME': str(iso)}) == {}
        # Still nothing once the launcher writes an empty map there.
        target.write_text('{"mcpServers":{}}')
        assert boxmcp.mcp_servers(agy, {'HOME': str(iso)}) == {}
        # And the file everyone else reads was never touched.
        assert json.loads(shared.read_text()) == yours
print('ok')
`;
  assert.equal(execFileSync("python3", ["-B", "-c", program], { cwd: root, encoding: "utf8" }).trim(), "ok");
});
