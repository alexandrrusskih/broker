// `update` walks the ways a harness can be updated until one works. The install is
// recognised from the path, because codex's own updater cannot do it once the wrapper
// starts the binary directly (it saw /opt/homebrew and tried a cask that is not there).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { root, temp, engine } = require("./helpers");

const stubs = async (t, names) => {
  const dir = await temp(t);
  for (const name of names) {
    fs.writeFileSync(path.join(dir, name), "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  }
  return dir;
};

const methods = (target, pathDir) => JSON.parse(engine(`
import json, os
from broker.providers import codex
os.path.realpath = lambda p: p
os.environ["PATH"] = ${JSON.stringify(pathDir)}
print(json.dumps([[n, a] for n, a in codex.update_methods(${JSON.stringify(target)})]))
`));

test("an npm install under the Homebrew prefix is updated with npm, aimed at that prefix", async (t) => {
  const bin = await stubs(t, ["npm", "brew"]);
  const target = "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/x/bin/codex";
  assert.deepEqual(methods(target, bin), [
    ["npm", ["npm", "install", "-g", "--prefix", "/opt/homebrew", "@openai/codex@latest"]],
    ["codex update", [target, "update"]]
  ]);
});

test("a cask goes to brew, a standalone install to its own updater, the unknown to codex update", async (t) => {
  const bin = await stubs(t, ["npm", "brew"]);
  const cask = "/opt/homebrew/Caskroom/codex/1.0/codex";
  assert.deepEqual(methods(cask, bin).map(([n]) => n), ["brew cask", "codex update"]);
  const standalone = "/Users/x/.codex/packages/standalone/current/bin/codex";
  assert.deepEqual(methods(standalone, bin), [["standalone", [standalone, "update"]]]);
  assert.deepEqual(methods("/somewhere/else/codex", bin).map(([n]) => n), ["codex update"]);
});

test("a way whose tool is not installed is not offered", async (t) => {
  const bin = await stubs(t, []);
  const target = "/usr/local/lib/node_modules/@openai/codex/bin/codex.js";
  assert.deepEqual(methods(target, bin).map(([n]) => n), ["codex update"]);
});

// update_harness against a stand-in provider: each way is a script that records it ran.
const walk = (dir, ways, extra = "") => JSON.parse(engine(`
import json, os
from broker import run
class P:
    NAME = BIN = "fake"
    PATH_SHIELD = False
    ${extra}
ways = ${JSON.stringify(ways)}
P.update_methods = staticmethod(lambda t: [(n, ["sh", "-c", c]) for n, c in ways])
run.real_bin = lambda p: "/bin/echo"
run._version = lambda t: "1.0"
code = run.update_harness(P)
print(json.dumps({"code": code, "ran": sorted(os.listdir(${JSON.stringify(dir)}))}))
`));

test("the first way that works ends the walk; failures fall through to the next", async (t) => {
  const dir = await temp(t);
  const ran = (n, code) => `touch ${dir}/${n}; exit ${code}`;
  const out = walk(dir, [["a", ran("a", 1)], ["b", ran("b", 0)], ["c", ran("c", 0)]]);
  assert.deepEqual(out, { code: 0, ran: ["a", "b"] });
});

test("when every way fails the update fails, and every way was tried", async (t) => {
  const dir = await temp(t);
  const ran = (n) => `touch ${dir}/${n}; exit 3`;
  assert.deepEqual(walk(dir, [["a", ran("a")], ["b", ran("b")]]), { code: 1, ran: ["a", "b"] });
});

test("a provider that knows nothing of its installs is updated with its own `update`, as before", async (t) => {
  const dir = await temp(t);
  const out = JSON.parse(engine(`
import json, os
from broker import run
class P:
    NAME = BIN = "fake"
    PATH_SHIELD = False
target = os.path.join(${JSON.stringify(dir)}, "harness")
open(target, "w").write("#!/bin/sh\\necho $@ > " + target + ".args\\n")
os.chmod(target, 0o755)
run.real_bin = lambda p: target
run._version = lambda t: "1.0"
print(json.dumps({"code": run.update_harness(P), "args": open(target + ".args").read().strip()}))
`));
  assert.deepEqual(out, { code: 0, args: "update" });
});

test("a bare `update` takes the walk; `update` with arguments goes to the harness untouched", () => {
  const out = JSON.parse(engine(`
import json
from broker import cli, run
calls = []
run.update_harness = lambda p: calls.append("walk") or 0
run.exec_passthrough = lambda p, a: calls.append("passthrough:" + " ".join(a)) or 0
cli.main("codex", ["update"])
cli.main("codex", ["update", "--check"])
print(json.dumps(calls))
`));
  assert.deepEqual(out, ["walk", "passthrough:update --check"]);
});

test("a curl-installed codex (current -> releases/N) is still updated by its own updater, nothing else", async (t) => {
  const home = await temp(t);
  const release = path.join(home, ".codex/packages/standalone/releases/1.0/bin");
  fs.mkdirSync(release, { recursive: true });
  fs.symlinkSync(path.join(home, ".codex/packages/standalone/releases/1.0"), path.join(home, ".codex/packages/standalone/current"));
  const target = path.join(home, ".codex/packages/standalone/current/bin/codex");
  const bin = await stubs(t, ["npm", "brew"]);
  const out = JSON.parse(engine(`
import json, os
from broker.providers import codex
os.environ["PATH"] = ${JSON.stringify(bin)}
print(json.dumps([n for n, a in codex.update_methods(${JSON.stringify(target)})]))
`));
  assert.deepEqual(out, ["standalone"]);
});
