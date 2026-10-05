const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const { sourcePackage } = require("../lib/source");
const root = path.join(__dirname, "..");

test("shell installer keeps the default client-only and forwards explicit server options", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-install-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const pkg = path.join(dir, "checkout with spaces");
  const bin = path.join(dir, "bin");
  await fs.mkdir(pkg);
  await fs.mkdir(bin);
  await fs.writeFile(path.join(pkg, "package.json"), "{}");
  await fs.writeFile(path.join(pkg, "cli.js"), "console.log('CLI_ARGS=' + JSON.stringify(process.argv.slice(2)))");
  for (const [name, content] of Object.entries({
    bun: "#!/bin/sh\nprintf 'FAKE_BUN'; printf ' <%s>' \"$@\"; printf '\\n'\n",
    git: "#!/bin/sh\nexit 99\n", sudo: "#!/bin/sh\nexit 99\n",
    uname: "#!/bin/sh\nprintf 'Darwin\\n'\n", id: "#!/bin/sh\nprintf '501\\n'\n"
  })) await fs.writeFile(path.join(bin, name), content, { mode: 0o755 });
  const env = { ...process.env, PATH: bin + path.delimiter + process.env.PATH };
  const run = (args) => execFileSync("/bin/bash", [path.join(root, "install.sh"), "--from", pkg, ...args], { env, encoding: "utf8", stdio: "pipe" });
  const parsed = (text) => JSON.parse(text.split("\n").find((l) => l.startsWith("CLI_ARGS=")).slice(9));
  assert.deepEqual(parsed(run([])), ["setup"]);
  assert.deepEqual(parsed(run(["--no-ask", "--client-config", "/test/client.json"])), ["setup", "--no-ask", "--client-config", "/test/client.json"]);
  const args = parsed(run(["--server", "--no-ask", "--url", "https://broker.test", "--port", "9000", "--data-dir", "/test/data"]));
  assert.deepEqual(args, ["server", "install", "--from", await fs.realpath(pkg), "--url", "https://broker.test", "--no-ask", "--data-dir", "/test/data", "--port", "9000"]);
  assert.equal(sourcePackage({}, pkg), pkg);
  for (const args of [["--unknown"], ["--url"], ["--port", "9000"], ["--server", "--client-config", "/test/client.json"]]) {
    assert.throws(() => run(args), (err) => err.status === 1 && !String(err.stdout).includes("FAKE_BUN"));
  }
});

test("upgrade changes the server only with --server; --all keeps its harness-update meaning", async () => {
  const source = (await fs.readFile(path.join(root, "cli.js"), "utf8")).replace("main().catch", "globalThis.done = main().catch");
  for (const flags of [[], ["--all"], ["--server"], ["--all", "--server"]]) {
    const calls = [];
    const output = [];
    const pkg = "/test/new checkout";
    const cfg = { shims: ["codex", "opencode"] };
    const context = {
      process: { argv: ["node", "cli.js", "upgrade", ...flags, "--from", pkg], execPath: "/test/node", stdout: { write() {} }, stderr: { write(s) { throw new Error(s); } }, exit() { assert.fail("unexpected exit"); } },
      console: { log(s) { output.push(s); }, error(s) { output.push(s); } },
      require(name) {
        if (name === "./lib/config") return { read: () => cfg };
        // DROP AFTER 2026-12 along with the module itself.
        if (name === "./lib/legacy") return { dropCaches: () => calls.push(["legacy.dropCaches"]) };
        if (name === "./package.json") return { version: "test" };
        if (name === "child_process") return {
          execSync: (cmd) => calls.push([cmd]),
          execFileSync: (cmd, args) => { calls.push([cmd, ...args]); return args[0] === "--version" ? Buffer.from(`${cmd} 1.2.3\n`) : undefined; }
        };
        if (name === "os") return { homedir: () => "/test/user" };
        if (name === "path") return path;
        if (name === "fs") return { existsSync: (p) => p === path.join(pkg, "lib", "service.js") };
        if (name === "./lib/source") return { sourcePackage: (_cfg, from) => { assert.equal(from, pkg); return pkg; } };
        if (name === "./lib/wrap") return { WRAP: {
          codex: { cmd: "broker-cx", bin: "codex" },
          opencode: { cmd: "broker-oc", bin: "opencode", updateCommand: "upgrade" }
        } };
        if (name === "./lib/service") return { createServiceManager: () => ({ requireInstalled: async () => calls.push(["check-server-installed"]) }) };
        throw new Error(`unexpected module ${name}`);
      }
    };
    vm.runInNewContext(source, context);
    await context.done;
    const hasServer = flags.includes("--server");
    assert.equal(calls.some((c) => c[0] === "check-server-installed"), hasServer);
    assert.equal(calls.some((c) => c[0] === "/test/node" && c[2] === "server"), hasServer);
    assert.equal(calls.some((c) => c[0] === "broker-cx" && c[1] === "update"), flags.includes("--all"));
    assert.equal(calls.some((c) => c[0] === "broker-oc" && c[1] === "upgrade"), flags.includes("--all"));
    assert.equal(calls.some((c) => c[0] === "broker-oc" && c[1] === "update"), false);
    if (hasServer) assert.deepEqual(calls.at(-1), ["/test/node", path.join(pkg, "cli.js"), "server", "install", "--no-ask", "--from", pkg]);
    assert.ok(calls.some((c) => c[0] === "bun" && c[1] === "install" && c[3] === pkg));
    for (const name of ["codex", "opencode"]) {
      assert.ok(calls.some((c) => c[0] === "/test/node" &&
        c[1] === path.join(pkg, "cli.js") && c[2] === "install" &&
        c[3] === name && c.includes("--quiet")));
    }
    assert.equal(output.some((line) => line.includes("undo the shim")), false);
    if (flags.includes("--all")) assert.ok(output.some((line) => line.startsWith("opencode: broker-oc 1.2.3 →")));
  }
});

// `#!/usr/bin/env python3` resolves against whatever PATH the caller has, and a
// prepared subprocess with PATH=/usr/bin:/bin gets macOS's own 3.9. The engine
// needs 3.11 for TOML, so every wrapper died there with "cannot find the broker
// engine (No module named 'tomllib')" — a message that names neither the real
// problem nor a fix that works.
test("a host wrapper names an interpreter that can actually run the engine", async (t) => {
  const { WRAP, pythonForLauncher } = require("../lib/wrap");

  // What the install writes into the shebang. Tested by import, not by version
  // string: the import is the thing that was failing.
  const chosen = pythonForLauncher();
  assert.ok(chosen, "this machine should have some python3 with tomllib");
  assert.ok(path.isAbsolute(chosen), "the shebang needs an absolute path, not a name");
  execFileSync(chosen, ["-c", "import tomllib"], { stdio: "ignore" });

  // Every python launcher must say its OWN name. A copied prefix sends people to
  // the wrong `broker wrap`, which fixes nothing — broker-oc reported itself as
  // broker-agy and told you to re-wrap agy.
  for (const provider of Object.keys(WRAP).filter((n) => WRAP[n].template)) {
    const template = await fs.readFile(
      path.join(root, "lib", "wrappers", WRAP[provider].template), "utf8");
    assert.ok(template.includes(`${WRAP[provider].cmd}: cannot find the broker engine`),
      `${provider}: launcher reports itself as something else`);
    assert.ok(template.includes(`broker wrap ${provider}`),
      `${provider}: launcher points at the wrong wrap command`);
  }

  // An image build keeps env lookup: a host path means nothing in a container.
  // Not exercised through install() here on purpose — install(.., binDirOverride)
  // REMEMBERS that directory in the broker config, and a test that did so pointed
  // every later install at its own temporary directory, which it had deleted.
  // The template itself is what a container gets, so assert on the template.
  const template = await fs.readFile(
    path.join(root, "lib", "wrappers", WRAP.codex.template), "utf8");
  assert.equal(template.split("\n")[0], "#!/usr/bin/env python3",
    "the shipped template stays portable; only a host install rewrites it");
});

// Loading the engine must not need 3.11 on paths that read no TOML. One
// module-level `import tomllib` in box/http_mcp.py made the whole broker need it,
// because box/run.py imports that module and every wrapper imports box.
test("the engine loads on a python without tomllib", () => {
  const old = ["/usr/bin/python3", "/usr/local/bin/python3"].find((p) => {
    try {
      execFileSync(p, ["-c", "import tomllib"], { stdio: "ignore" });
      return false;
    } catch (error) {
      return require("node:fs").existsSync(p);
    }
  });
  if (!old) return; // every python here is new enough; nothing to prove
  execFileSync(old, ["-c",
    "import sys; sys.path.insert(0, 'lib/wrappers')\n" +
    "from broker.cli import main\n" +
    "from broker.box import http_mcp, run\n"], { cwd: root, stdio: "pipe" });
});
