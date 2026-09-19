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
    const pkg = "/test/new checkout";
    const cfg = { shims: ["codex"] };
    const context = {
      process: { argv: ["node", "cli.js", "upgrade", ...flags, "--from", pkg], execPath: "/test/node", stdout: { write() {} }, stderr: { write(s) { throw new Error(s); } }, exit() { assert.fail("unexpected exit"); } },
      console: { log() {} },
      require(name) {
        if (name === "./lib/config") return { read: () => cfg };
        // DROP AFTER 2026-12 along with the module itself.
        if (name === "./lib/legacy") return { dropCaches: () => calls.push(["legacy.dropCaches"]) };
        if (name === "./package.json") return { version: "test" };
        if (name === "child_process") return {
          execSync: (cmd) => calls.push([cmd]),
          execFileSync: (cmd, args) => calls.push([cmd, ...args])
        };
        if (name === "os") return { homedir: () => "/test/user" };
        if (name === "path") return path;
        if (name === "fs") return { existsSync: (p) => p === path.join(pkg, "lib", "service.js") };
        if (name === "./lib/source") return { sourcePackage: (_cfg, from) => { assert.equal(from, pkg); return pkg; } };
        if (name === "./lib/wrap") return { WRAP: { codex: { cmd: "broker-cx", bin: "codex" } } };
        if (name === "./lib/service") return { createServiceManager: () => ({ requireInstalled: async () => calls.push(["check-server-installed"]) }) };
        throw new Error(`unexpected module ${name}`);
      }
    };
    vm.runInNewContext(source, context);
    await context.done;
    const hasServer = flags.includes("--server");
    assert.equal(calls.some((c) => c[0] === "check-server-installed"), hasServer);
    assert.equal(calls.some((c) => c[0] === "/test/node"), hasServer);
    assert.equal(calls.some((c) => c[0] === "broker-cx" && c[1] === "update"), flags.includes("--all"));
    if (hasServer) assert.deepEqual(calls.at(-1), ["/test/node", path.join(pkg, "cli.js"), "server", "install", "--no-ask", "--from", pkg]);
    assert.ok(calls.some((c) => c[0] === "bun" && c[1] === "install" && c[3] === pkg));
    assert.ok(calls.some((c) => c[0] === "broker" && c[1] === "codex" && c[2] === "install"));
  }
});
