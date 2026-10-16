// What a box is: the arguments it takes out, the home and project it carries,
// and the credentials it does not.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { root, temp, engine } = require("./helpers");

test("--box is taken out of the arguments, and everything after -- is left alone", () => {
  const out = engine(`
from broker import box
from broker.box import boxes, mcp, run, sync
import json
print(json.dumps([
  box.take_flag(["--box", "work", "-p", "hi"]),
  box.take_flag(["--box=work", "--resume", "abc"]),
  box.take_flag(["-p", "no box here"]),
  box.take_flag(["--box", "work", "--", "--box", "this is a prompt"]),
]))
`);
  assert.deepEqual(JSON.parse(out), [
    ["work", ["-p", "hi"]],
    ["work", ["--resume", "abc"]],
    [null, ["-p", "no box here"]],
    // A prompt that mentions --box is a prompt. Only the flag before -- is ours.
    ["work", ["--", "--box", "this is a prompt"]],
  ]);
});

test("--box . is the repository you stand in, or the box that already lists it", async (t) => {
  const dir = await fs.realpath(await temp(t));
  const git = (...args) => require("node:child_process").execFileSync("git", args, { stdio: "ignore" });
  const repo = path.join(dir, "Work", "new-thing");
  const listed = path.join(dir, "Work", "listed");
  const plain = path.join(dir, "Scratch", "notes");
  await fs.mkdir(path.join(repo, "src"), { recursive: true });
  await fs.mkdir(listed, { recursive: true });
  await fs.mkdir(plain, { recursive: true });
  git("-C", repo, "init", "-q");
  git("-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x");
  const worktree = path.join(dir, "wt", "feature");
  git("-C", repo, "worktree", "add", "-q", worktree);
  await fs.writeFile(path.join(dir, "boxes.json"), `{
    // comments stay fine
    "__defaults": {"env": {"GIT_AUTHOR_NAME": "Me", "SHARED": "template"}, "ro": ["~/ref"]},
    "listed": {"rw": [${JSON.stringify(listed)}], "env": {"SHARED": "box"}, "ro": ["~/more"]},
    "machines": {"build": {"ssh": "me@build"}}
  }`);

  const out = engine(`
import json
from broker.box import boxes
boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
def ask(name, cwd):
    try:
        return list(boxes.resolve(name, cwd))
    except SystemExit:
        return "refused"
print(json.dumps({
    "sub": ask(".", ${JSON.stringify(path.join(repo, "src"))}),
    "listed": ask(".", ${JSON.stringify(listed)}),
    "named": ask("listed", ${JSON.stringify(plain)}),
    "plain": ask(".", ${JSON.stringify(plain)}),
    "worktree": ask(".", ${JSON.stringify(worktree)}),
    "home": ask(".", ${JSON.stringify(dir)}),
    "names": sorted(boxes.profiles()),
}))
`, { HOME: dir, BROKER_REAL_HOME: dir });
  const seen = JSON.parse(out);

  // From a subdirectory, the whole repository — the same box as from its root.
  const [name, profile, asked] = seen.sub;
  assert.match(name, /^new-thing-[0-9a-f]{6}$/);
  assert.equal(asked, ".", "the way back is typed as it was asked");
  assert.deepEqual(profile.rw, [repo]);
  assert.deepEqual(profile.env, { GIT_AUTHOR_NAME: "Me", SHARED: "template" });
  assert.deepEqual(profile.ro, ["~/ref"]);

  // A project a box already lists is that box, under its own name.
  assert.equal(seen.listed[0], "listed");
  assert.equal(seen.listed[2], "listed");
  // Every named box starts from the template: its own value wins per key, and
  // its lists add to the template's.
  assert.deepEqual(seen.named[1].env, { GIT_AUTHOR_NAME: "Me", SHARED: "box" });
  assert.deepEqual(seen.named[1].ro, ["~/ref", "~/more"]);

  assert.deepEqual(seen.plain[1].rw, [plain], "outside a repository, the directory itself");
  // A worktree's history lives in the main repository's .git.
  assert.deepEqual(seen.worktree[1].rw, [worktree, path.join(repo, ".git")]);
  assert.notEqual(seen.worktree[0], name, "another checkout is another box");
  assert.equal(seen.home, "refused", "never the whole home");
  assert.deepEqual(seen.names, ["listed"], "the template and the machines are not boxes");
});

test("a box carries the other harnesses it names, each as it would carry its own", async (t) => {
  const dir = await fs.realpath(await temp(t));
  const project = path.join(dir, "project");
  const codexProfile = path.join(dir, ".codex-main");
  await fs.mkdir(project);
  await fs.mkdir(path.join(dir, ".claude"));
  await fs.writeFile(path.join(dir, ".claude.json"), "{}");
  await fs.mkdir(path.join(dir, ".codex"));
  await fs.writeFile(path.join(dir, ".codex", "config.toml"), "");
  await fs.mkdir(codexProfile);
  await fs.writeFile(path.join(codexProfile, "auth.json"), "{}");
  await fs.mkdir(path.join(dir, ".gemini", "antigravity-cli"), { recursive: true });
  await fs.mkdir(path.join(dir, ".local", "share", "opencode"), { recursive: true });
  await fs.writeFile(path.join(dir, ".local", "share", "opencode", "auth.json"), "{}");

  const out = engine(`
import json, os
from broker import box, config
from broker.box import run
from broker.providers import claude, codex, agy, opencode
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
base = {"BROKER_ACTIVE": "claude:main", "CLAUDE_CODE_OAUTH_TOKEN": "claude-token"}
cmd = run.command(claude, "demo", {"rw": [${JSON.stringify(project)}], "mcp": False}, ["-p", "hi"], base,
                  companions=[(codex, dict(base, CODEX_HOME=${JSON.stringify(codexProfile)},
                                           CODEX_REFRESH_TOKEN_URL_OVERRIDE="https://broker.example.ts.net/oauthRefresh?account=main")),
                              (agy, dict(base, HOME=${JSON.stringify(path.join(dir, ".agy", "main"))})),
                              (opencode, dict(base))])
print(json.dumps(cmd))
`, { HOME: dir, BROKER_REAL_HOME: dir });
  const cmd = JSON.parse(out);
  const line = cmd.join(" ");

  assert.ok(line.includes(`target=${dir}/.claude,`) || line.includes(`target=${dir}/.claude `), "its own home");
  assert.ok(line.includes(`target=${dir}/.codex`), "codex's directory comes along");
  assert.ok(line.includes(`target=${codexProfile}`), "with the account's profile");
  assert.ok(line.includes(`-e CODEX_HOME=${codexProfile}`), "and codex is pointed at it");
  assert.ok(line.includes(`target=${dir}/.gemini`), "agy's directory comes along");
  assert.ok(line.includes(`target=${dir}/.local/share/opencode/auth.json`), "opencode's login, as a copy");
  assert.ok(line.includes("-e CLAUDE_CODE_OAUTH_TOKEN=claude-token"));
  // What a companion's account set reaches the box, so codex can renew there...
  assert.ok(line.includes("-e CODEX_REFRESH_TOKEN_URL_OVERRIDE=https://broker.example.ts.net/oauthRefresh?account=main"));
  // ...but not the profile agy is handed as $HOME: inside, $HOME is your home.
  assert.ok(!line.includes(`-e HOME=${dir}/.agy/main`));

  // Docker refuses a container that names one mount point twice.
  const targets = [];
  cmd.forEach((part, i) => {
    if (cmd[i - 1] === "--mount") targets.push(part.match(/target=([^,]+)/)[1]);
    if (cmd[i - 1] === "--tmpfs") targets.push(part.split(":")[0]);
  });
  assert.deepEqual(targets.filter((x, i) => targets.indexOf(x) !== i), [], "no mount point twice");

  // The box still runs the harness that started it.
  assert.deepEqual(cmd.slice(-5), ["broker-box-entry", "claude", "--dangerously-skip-permissions", "-p", "hi"]);
});

test("each companion gets its own account, and one without an account is left out", async (t) => {
  const dir = await fs.realpath(await temp(t));
  const out = engine(`
import json, sys
from broker import config, select
from broker.box import companions
from broker.providers import claude
config.load = lambda die: {"url": "https://broker.example.ts.net", "key": "k"}
def resolve(cfg, provider, explicit=None, preferred=None):
    if provider.NAME == "agy":
        from broker.out import die
        die("no agy accounts seeded")
    return "main", {"tokens": {"access_token": "a", "refresh_token": "f" * 64, "id_token": "i"}}
select.resolve = resolve
def ask(names):
    try:
        found = companions.companions(claude, {"harnesses": names}, {"HOME": ${JSON.stringify(dir)}})
    except SystemExit:
        return "refused"
    return [[p.NAME, sorted(k for k in env if k.startswith(("CODEX_", "BROKER_ACTIVE")))] for p, env in found]
print(json.dumps({
    "all": ask(["claude", "codex", "agy", "opencode"]),
    "none": ask(None),
    "off": ask(False),
    "bogus": ask(["codex", "gpt"]),
}))
`, { HOME: dir, BROKER_REAL_HOME: dir });
  const seen = JSON.parse(out);
  // The box's own harness is not carried twice; agy has no account and is
  // left out; opencode holds its own login and needs none from the broker.
  assert.deepEqual(seen.all.map(([name]) => name), ["codex", "opencode"]);
  assert.ok(seen.all[0][1].includes("BROKER_ACTIVE"), "codex knows it is brokered");
  assert.ok(seen.all[0][1].includes("CODEX_HOME"), "codex is pointed at its account's profile");
  assert.ok(seen.all[0][1].includes("CODEX_REFRESH_TOKEN_URL_OVERRIDE"), "and renews through the broker");
  assert.deepEqual(seen.none, [], "nothing named, nothing carried");
  assert.deepEqual(seen.off, [], "false carries nothing either");
  assert.equal(seen.bogus, "refused");
});

test("__defaults may not name a place to write or a Dockerfile", async (t) => {
  const dir = await temp(t);
  for (const key of ["rw", "dockerfile"]) {
    await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({ __defaults: { [key]: ["~/x"] }, a: {} }));
    const out = engine(`
from broker.box import boxes
boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
try:
    boxes.profiles()
    print("accepted")
except SystemExit:
    print("refused")
`, { HOME: dir });
    assert.equal(out.trim(), "refused", key);
  }
});

test("the box carries the harness's own directory and the project, with the token only in the environment", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const reference = path.join(dir, "reference");
  await fs.mkdir(project);
  await fs.mkdir(reference);

  const out = engine(`
import json, os
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import claude
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(claude, "demo",
                  {"rw": [${JSON.stringify(project)}], "ro": [${JSON.stringify(reference)}]},
                  ["-p", "hi"],
                  {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "BROKER_ACTIVE": "claude:sk"})
print(json.dumps(cmd))
`);
  const cmd = JSON.parse(out);
  const line = cmd.join(" ");
  const home = os.homedir();

  assert.equal(cmd[1], "run");
  assert.ok(line.includes(`--user ${process.getuid()}:${process.getgid()}`), "runs as you, so new files are yours");
  assert.ok(line.includes(`--tmpfs ${home}:uid=${process.getuid()}`), "$HOME must be writable inside");

  // Same path inside as outside — session history is keyed by it.
  assert.ok(line.includes(`source=${project},target=${project}`), "the project keeps its path");
  assert.ok(line.includes(`source=${reference},target=${reference},readonly`), "ro stays ro");
  assert.ok(line.includes(`source=${home}/.claude,target=${home}/.claude`), "settings, MCP and history come along");

  // The account's token rides in the environment and is never written to disk.
  assert.ok(line.includes("-e CLAUDE_CODE_OAUTH_TOKEN=fake-token"));
  // .credentials.json is NOT that token — it holds the logins for the MCP
  // servers, which are the same services whichever account is picked. Covering
  // it with a read-only empty file started every box logged out of all of them,
  // with nowhere to save a new login.
  assert.ok(!line.includes(`target=${home}/.claude/.credentials.json`),
    "MCP logins travel with the directory, and a login inside a box sticks");
  // Image, entry point, harness, the engine's own danger flag (a box is the
  // externally sandboxed environment those flags are for), then exactly what
  // you typed — nothing rewritten.
  assert.deepEqual(cmd.slice(-6),
    ["broker-box", "broker-box-entry", "claude", "--dangerously-skip-permissions", "-p", "hi"]);
});

test("a file-credentials harness gets its per-account profile, not the shared directory's token", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const profile = path.join(dir, "codex-sk");
  await fs.mkdir(project);
  await fs.mkdir(profile);
  await fs.writeFile(path.join(profile, "auth.json"), "profile-token");

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import codex
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(codex, "demo", {"rw": [${JSON.stringify(project)}]}, ["exec", "hi"],
                  {"CODEX_HOME": ${JSON.stringify(profile)}, "BROKER_ACTIVE": "codex:sk"})
print(json.dumps(cmd))
`);
  const line = JSON.parse(out).join(" ");
  const mount = JSON.parse(out).find((part) => part.includes(`target=${profile}`));
  assert.ok(mount, "the account's profile comes in");
  const source = mount.match(/source=([^,]+)/)[1];
  assert.notEqual(source, profile, "the box cannot replace a host profile symlink");
  assert.equal(await fs.readFile(path.join(source, "auth.json"), "utf8"), "profile-token");
  assert.ok(line.includes(`-e CODEX_HOME=${profile}`), "and the harness is pointed at it");
  assert.ok(line.includes(`target=${os.homedir()}/.codex/auth.json,readonly`), "the shared directory's own token is covered");
});

test("codex renews its token at a broker the box can reach", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const profile = path.join(dir, "codex-main");
  await fs.mkdir(project);
  await fs.mkdir(profile);
  await fs.writeFile(path.join(profile, "auth.json"), "{}");
  const run = (url, remote) => JSON.parse(engine(`
import json
from broker import box
from broker.providers import codex
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
q = "?provider=codex&account=main"
cmd = box.command(codex, "demo", {"rw": [${JSON.stringify(project)}]}, ["exec", "hi"],
                  {"CODEX_HOME": ${JSON.stringify(profile)}, "BROKER_ACTIVE": "codex:main",
                   "CODEX_REFRESH_TOKEN_URL_OVERRIDE": ${JSON.stringify(url)} + "/oauthRefresh" + q,
                   "CODEX_REVOKE_TOKEN_URL_OVERRIDE": ${JSON.stringify(url)} + "/oauthRevoke" + q},
                  remote=${remote ? JSON.stringify(remote) : "False"})
print(json.dumps(cmd))
`, { CODEX_REFRESH_TOKEN_URL_OVERRIDE: "", CODEX_REVOKE_TOKEN_URL_OVERRIDE: "" }));
  // Docker keeps the LAST -e of a name, so that is what the harness sees.
  const last = (cmd, name) => cmd.filter((part) => part.startsWith(`${name}=`)).pop();

  // A broker on this machine's loopback is the container's own inside a box.
  const local = run("http://127.0.0.1:8787");
  assert.equal(last(local, "CODEX_REFRESH_TOKEN_URL_OVERRIDE"),
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE=http://host.docker.internal:8787/oauthRefresh?provider=codex&account=main");
  assert.equal(last(local, "CODEX_REVOKE_TOKEN_URL_OVERRIDE"),
    "CODEX_REVOKE_TOKEN_URL_OVERRIDE=http://host.docker.internal:8787/oauthRevoke?provider=codex&account=main");
  assert.ok(local.join(" ").includes("--add-host host.docker.internal:host-gateway"));

  // A tailnet name already reaches it from anywhere, and is left as it is.
  const tailnet = run("https://broker.example.ts.net");
  assert.equal(last(tailnet, "CODEX_REFRESH_TOKEN_URL_OVERRIDE"),
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE=https://broker.example.ts.net/oauthRefresh?provider=codex&account=main");

  // On another machine the host gateway is THAT machine: nothing to rewrite to.
  const far = run("http://127.0.0.1:8787", "big");
  assert.equal(last(far, "CODEX_REFRESH_TOKEN_URL_OVERRIDE"),
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE=http://127.0.0.1:8787/oauthRefresh?provider=codex&account=main");
});

test("two hosts may share one key without the box refusing to start", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  const key = path.join(dir, "id_shared");
  await fs.mkdir(project);
  await fs.writeFile(key, "private key");
  await fs.writeFile(path.join(dir, "boxes.json"), JSON.stringify({
    demo: {
      rw: [project],
      ssh: { hosts: { "github.com": key, "git.example.com": key } },
    },
  }));

  const out = engine(`
import json
from broker import box
from broker.box import boxes
from broker.providers import claude
box.boxes.PATH = ${JSON.stringify(path.join(dir, "boxes.json"))}
cmd = box.command(claude, "demo", boxes.profiles()["demo"], [], {})
print(json.dumps([m for m in cmd if ${JSON.stringify(key)} in m]))
`, { BROKER_CONFIG_DIR: dir, HOME: dir });

  // Mounted once, however many hosts name it. Twice and docker refuses the
  // whole run — "Duplicate mount point" — so the box does not start at all.
  assert.equal(JSON.parse(out).length, 1, "one key, one mount");
});

test("a harness the broker holds no credentials for still gets a box", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(path.join(dir, ".local", "share", "opencode"), { recursive: true });
  await fs.writeFile(path.join(dir, ".local", "share", "opencode", "opencode.db"), "x");
  await fs.mkdir(project, { recursive: true });

  const out = engine(`
import json
from broker import box
from broker.box import boxes, mcp, run, sync
from broker.providers import opencode
opencode.MCP_CONFIG = None
print(json.dumps(box.command(opencode, "demo", {"rw": ["${project}"]}, ["run", "hi"], {})))
`, { HOME: dir });

  const line = JSON.parse(out).join(" ");
  // Its database is one file for every session it has ever had, and the
  // journal lives beside it: shared across the boundary it tears, which is how
  // a day of another harness's history was lost. So the box gets a clone.
  assert.ok(!/source=.*\.local\/share\/opencode\/opencode\.db,/.test(line.replace(/box\/private[^,]*/g, "")),
    "the real database must not be mounted");
  assert.match(line, /box\/private\/demo\/opencode\/opencode\.db/, line);
  // Mounting a file deep under $HOME has the container create its parents as
  // root, and the harness — which runs as you — then cannot write beside its
  // own database.
  assert.match(line, new RegExp(`--tmpfs ${path.join(dir, ".local")}:uid=`), line);
});

test("the MCP logins of the machine are not the box's to empty", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);

  const out = engine(`
import json
from broker import box
from broker.providers import agy
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(agy, "demo", {"rw": [${JSON.stringify(project)}]}, ["hi"], {})
print(json.dumps(cmd))
`);
  const line = JSON.parse(out).join(" ");
  const home = os.homedir();
  const tokens = `${home}/.gemini/antigravity-cli/mcp_oauth_tokens.json`;

  // This harness rewrites that file whole, keeping only what its own session
  // logged in — so a box, which knows nothing, writes an empty object over the
  // logins of this machine. It gets its own instead.
  assert.ok(line.includes(`target=${tokens}`), "the box's view of the tokens is covered");
  assert.ok(!line.includes(`source=${tokens}`), "and it is not the machine's file underneath");
  // Not read-only: the harness would fail on a write it expects to succeed,
  // instead of carrying on without the servers that need a login.
  assert.ok(!line.includes(`target=${tokens},readonly`), "a box may write its own");
});

// Nothing inside a box could tell it was in one: the paths match the host
// exactly, which is the point. A hook that must report the chat it is in to a
// terminal manager outside the container has to know to do that.
test("a box says that it is a box, and which one", async (t) => {
  const dir = await temp(t);
  const project = path.join(dir, "project");
  await fs.mkdir(project);
  const out = engine(`
import json
from broker import box
from broker.providers import claude
from broker import config
config.CONFIG_DIR = ${JSON.stringify(path.join(dir, "cfg"))}
cmd = box.command(claude, "work", {"rw": [${JSON.stringify(project)}], "mcp": False}, [], {})
print(json.dumps([cmd[i + 1] for i, part in enumerate(cmd) if part == "-e"]))
`);
  const env = JSON.parse(out);
  assert.ok(env.includes("BROKER_BOX=work"), "the box says its own name");
  // The bus hooks run from the harness settings this box mounts, on the host
  // and in here alike, and their box branch reads this one.
  assert.ok(env.includes("AGNTBUS_BOX=1"), "the bus hooks are told they are boxed");
});
