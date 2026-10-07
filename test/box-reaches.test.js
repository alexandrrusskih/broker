// What a box is allowed to reach: shared directories, a token for one host, a
// command replaced by a note, the flags it adds, and another machine.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { temp, engine } = require("./helpers");

test("a box reaches shared directories through every profile, not just this run's", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  // Sessions are shared already: each profile's directory is a link into the
  // canonical home.
  const canonical = path.join(dir, ".tool");
  await fs.mkdir(path.join(canonical, "sessions"), { recursive: true });
  for (const account of ["one", "two"]) {
    await fs.mkdir(path.join(dir, `.tool-${account}`), { recursive: true });
    await fs.symlink(path.join(canonical, "sessions"), path.join(dir, `.tool-${account}`, "sessions"));
  }

  const out = engine(`
import json
from broker import box
from broker.providers import codex
codex.MCP_CONFIG = None
codex.CANONICAL_HOME = "${canonical}"
codex.BOX_SHARED = ("sessions",)
print(json.dumps(box.command(codex, "demo", {"rw": ["${project}"]}, [], {"CODEX_HOME": "${path.join(dir, ".tool-one")}"})))
`, { HOME: dir });
  const line = JSON.parse(out).join(" ");

  // A harness records the path it saw, through whichever profile was current.
  // Resuming under another account then fails on a file that is right there.
  assert.ok(line.includes(`target=${dir}/.tool-one/sessions`), "this run's profile");
  assert.ok(line.includes(`target=${dir}/.tool-two/sessions`), "and the one it used to be");
  // Only that directory: another account's credentials stay out.
  assert.ok(!line.includes(`${dir}/.tool-two/auth`), "nothing else of another account");
});

test("a box that is given a GitLab token can prove it is logged in", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  await fs.writeFile(path.join(dir, "key"), "private key");
  await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({
    demo: { rw: [project], ssh: { hosts: { "git.example.com": path.join(dir, "key") } } },
    plain: { rw: [project] },
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes
from broker.providers import claude
box.boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
cmd = box.command(claude, "demo", boxes.profiles()["demo"], [],
                  {"GITLAB_TOKEN": "glpat-secret", "GITLAB_HOST": "git.example.com"})
mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
found = [m for m in mounts if "glab-cli/config.yml" in m]
print(json.dumps({"mount": found[0] if found else None,
                  "content": open(found[0].split("source=")[1].split(",")[0]).read() if found else ""}))
`, { BROKER_CONFIG_DIR: dir, HOME: dir });

  const got = JSON.parse(out);
  // The tool reads the token from the environment and works — but its own
  // status command looks for a known host, which on this machine is in the
  // Keychain and does not cross into a container. Anything that checks before
  // it acts stops on "has not been authenticated" over a login that is fine.
  assert.ok(got.mount, "the box is told which host it is logged in to");
  assert.match(got.content, /git\.example\.com/);
  assert.match(got.content, /token: glpat-secret/);

  // And a box that was NOT given a key to that host gets nothing, however the
  // token happens to be sitting in the environment — it reaches every box,
  // because the environment does.
  const bare = engine(`
import json
from broker import box
from broker.box import boxes
from broker.providers import claude
box.boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
cmd = box.command(claude, "plain", boxes.profiles()["plain"], [],
                  {"GITLAB_TOKEN": "glpat-secret", "GITLAB_HOST": "git.example.com"})
print(json.dumps([a for a in cmd if "glab" in a]))
`, { BROKER_CONFIG_DIR: dir, HOME: dir });
  assert.deepEqual(JSON.parse(bare), [], "no key to that host, no credential");
});

test("a box can stand in for a command that must not run inside it", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const out = engine(`
import json
from broker import box
from broker.box import store
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo", {
  "rw": ["${project}"],
  "stubs": {"~/bin/thing": "not in a box; use its MCP tools"},
}, [], {})))
`, { HOME: dir });

  // Deliberately absent is not the same as missing: an agent told to run a
  // command it cannot find searches the whole disk and then asks where it is.
  assert.match(JSON.parse(out).join(" "), /source=.*box\/stubs\/demo\/thing,target=.*bin\/thing,readonly/);
  const stub = await fs.readFile(path.join(dir, ".config", "broker", "box", "stubs", "demo", "thing"), "utf8");
  assert.match(stub, /not in a box; use its MCP tools/);
  assert.match(stub, /exit 127/, "it fails like a missing command, but says why");
});

test("a box decides what flags the harness gets, and yours still win", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const run = (args, argv) => JSON.parse(engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
claude.MCP_CONFIG = None
print(json.dumps(box.command(claude, "demo",
  {"rw": ["${project}"], "args": ${JSON.stringify(args)}}, ${JSON.stringify(argv)}, {})))
`, { HOME: dir }));

  // Per harness, because they spell the same idea differently.
  assert.deepEqual(run({ claude: ["--box-flag"] }, ["-p", "hi"]).slice(-4), ["claude", "--box-flag", "-p", "hi"]);
  assert.deepEqual(run({ codex: ["--other"] }, []).slice(-1), ["claude"], "another harness's flags are not ours");
  // A plain list is for every harness in the box.
  assert.deepEqual(run(["--everywhere"], []).slice(-2), ["claude", "--everywhere"]);
  // The box says it first, so the same flag typed by hand is the one that counts.
  assert.deepEqual(run({ claude: ["--mode"] }, ["--mode", "plan"]).slice(-3), ["claude", "--mode", "plan"],
    "a flag already typed is not added twice");
});

test("a box can run on another machine, and only its sources move", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project, { recursive: true });
  await fs.mkdir(path.join(dir, ".config", "broker"), { recursive: true });
  await fs.writeFile(path.join(dir, ".config", "broker", "boxes.json"), JSON.stringify({
    demo: { rw: [project] },
    machines: { windows: { ssh: "me@win", paths: { [project]: "D:/work/project" } } },
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
claude.MCP_CONFIG = None
box.boxes.PATH = "${path.join(dir, ".config", "broker", "boxes.json")}"
print(json.dumps(sorted(box.profiles())))
name, machine, rest = box.boxes.take_remote(["--remote", "windows", "-p", "hi"])
print(json.dumps([name, rest]))
cmd = box.command(claude, "demo", box.profiles()["demo"], rest, {})
print(json.dumps(box.start._over_ssh(cmd, machine, name)))
`, { HOME: dir });

  const [boxesLine, flagLine, sshLine] = out.trim().split("\n");
  // A machine is not a box, however it is spelled in the same file.
  assert.deepEqual(JSON.parse(boxesLine), ["demo"]);
  assert.deepEqual(JSON.parse(flagLine), ["windows", ["-p", "hi"]]);

  const sent = JSON.parse(sshLine).join(" ");
  assert.match(sent, /^ssh -t .*me@win --/, sent);
  // The source of a bind mount is a path on THAT machine...
  assert.match(sent, /source=D:\/work\/project,target=/, sent);
  // ...and everything inside the container stays exactly where it was, or
  // --resume and every path the harness remembers would break.
  assert.match(sent, new RegExp(`target=${project.replace(/[/\\]/g, "\\$&")}`), sent);
  assert.match(sent, new RegExp(`-w ${project.replace(/[/\\]/g, "\\$&")}`), sent);
});
