const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

// codex keys folder trust on the exact directory it starts in and inherits
// nothing from a parent. A box whose rw list is one wide tree therefore had
// the tree trusted and the directory inside it not, and the question came up
// on the first screen of a box that cannot answer it: config.toml travels
// read-only, so the write dies with -32603.
test("the directory a box starts in is trusted, not only its rw roots", async (t) => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "broker-trust-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const tree = path.join(home, "Projects");
  const project = path.join(tree, "kopanev-me");
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(path.join(home, ".codex"));
  await fs.writeFile(path.join(home, ".codex", "config.toml"),
    `[projects."${tree}"]\ntrust_level = "trusted"\n`);

  const engine = path.join(__dirname, "..", "lib", "wrappers");
  const code = `
import json, sys
sys.path.insert(0, ${JSON.stringify(engine)})
from broker.box import extras, run
from broker.providers import codex
extras._passwd_file = lambda *args: None
cmd = run.command(codex, "demo", {"rw": [${JSON.stringify(tree)}], "mcp": False}, [], {})
print(json.dumps([cmd[i + 1] for i, part in enumerate(cmd) if part == "--mount"]))
`;
  // Started INSIDE the rw tree, which is the case that was broken.
  const mounts = JSON.parse(execFileSync("python3", ["-c", code], {
    cwd: project, encoding: "utf8",
    env: { ...process.env, HOME: home, BROKER_REAL_HOME: home, PYTHONDONTWRITEBYTECODE: "1" },
  }));

  const target = path.join(home, ".codex", "config.toml");
  const mount = mounts.find((m) => m.includes(`target=${target},readonly`));
  assert.ok(mount, "codex's config.toml must be mounted read-only");
  const source = mount.match(/source=([^,]+)/)[1];
  assert.notEqual(source, target, "the box reads a staged copy, not the host's file");
  const staged = await fs.readFile(source, "utf8");
  // The physical path, which is what the box is given as its -w and so what
  // codex asks about inside it.
  const real = await fs.realpath(project);
  assert.match(staged, new RegExp(`^\\[projects\\."${real}"\\]$`, "m"));
  assert.equal(staged.match(/\[projects\./g).length, 2, "the tree's own entry is kept, once");
  // The host's file is the one the person decides in, and it is not touched.
  assert.equal(await fs.readFile(target, "utf8"),
    `[projects."${tree}"]\ntrust_level = "trusted"\n`);
});
