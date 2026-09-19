const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { writeJson, readJson } = require("../functions/stores/files");
const root = path.join(__dirname, "..");

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-client-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  return dir;
}

test("Node CLI and Python wrapper accept env-only config but preserve saved connection precedence", async (t) => {
  const dir = await temp(t);
  const file = path.join(dir, "client.json");
  const env = { ...process.env, BROKER_CONFIG: file, BROKER_URL: "https://runtime.example.test", BROKER_KEY: "fake-runtime-key", PYTHONDONTWRITEBYTECODE: "1" };
  const readNode = () => JSON.parse(execFileSync(process.execPath, ["-e", "process.stdout.write(JSON.stringify(require('./lib/config').require()))"], { cwd: root, env }));
  const pythonCode = "import sys,json; sys.path.insert(0, 'lib/wrappers'); from broker import config; print(json.dumps(config.load(sys.exit)))";
  const readPython = () => JSON.parse(execFileSync("python3", ["-c", pythonCode], { cwd: root, env }));
  for (const value of [readNode(), readPython()]) {
    assert.equal(value.url, env.BROKER_URL);
    assert.equal(value.key, env.BROKER_KEY);
  }
  await writeJson(file, { url: "https://saved.example.test", key: "fake-saved-key", account: "main" });
  for (const value of [readNode(), readPython()]) {
    assert.equal(value.url, "https://saved.example.test");
    assert.equal(value.key, "fake-saved-key");
    assert.equal(value.account, "main");
  }
  delete env.BROKER_URL;
  delete env.BROKER_KEY;
  for (const value of [readNode(), readPython()]) {
    assert.equal(value.url, "https://saved.example.test");
    assert.equal(value.key, "fake-saved-key");
  }
});

test("config updates are private and do not persist runtime credentials", async (t) => {
  const dir = await temp(t);
  const file = path.join(dir, "client.json");
  await writeJson(file, { key: "saved", url: "https://saved.example.test" });
  const env = { ...process.env, BROKER_CONFIG: file, BROKER_KEY: "do-not-persist", BROKER_URL: "https://runtime.example.test", PYTHONDONTWRITEBYTECODE: "1" };
  execFileSync(process.execPath, ["-e", "require('./lib/config').write({account:'main'})"], { cwd: root, env });
  assert.deepEqual(await readJson(file), { key: "saved", url: "https://saved.example.test", account: "main" });
  execFileSync("python3", ["-c", "import sys; sys.path.insert(0, 'lib/wrappers'); from broker import config; config.save({'account':'second'})"], { cwd: root, env });
  assert.deepEqual(await readJson(file), { key: "saved", url: "https://saved.example.test", account: "second" });
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
});

test("shim routes handles to private broker, leaves native login/legacy tokens alone", () => {
  const program = `
import sys
from unittest.mock import patch
sys.path.insert(0, 'lib/wrappers')
from broker.providers import codex
from broker import run, api
cfg = {'url': 'https://broker.example.test', 'key': 'fake'}
auth = {'tokens': {'refresh_token': 'a' * 64, 'access_token': 'fake'}}
env = {}
codex.route_refresh(env, cfg, 'main / one', auth)
assert env['CODEX_REFRESH_TOKEN_URL_OVERRIDE'] == 'https://broker.example.test/oauthRefresh?provider=codex&account=main+%2F+one'
assert '/oauthRevoke?' in env['CODEX_REVOKE_TOKEN_URL_OVERRIDE']
codex.route_refresh(env, cfg, 'main', {'tokens': {'refresh_token': 'legacy-real-format'}})
assert 'CODEX_REFRESH_TOKEN_URL_OVERRIDE' not in env
assert 'CODEX_REVOKE_TOKEN_URL_OVERRIDE' not in env
codex.route_refresh(env, cfg, 'main', auth)
codex.login_env(env)
assert not env
assert api.TIMEOUT > 62
# Exercise the actual exec handoff without running Codex or touching its home.
with patch.object(codex, 'HOME_ENV', 'BROKER_TEST_PROFILE'), patch.object(run.profile, 'profile_dir', return_value='/test/profile'), patch.object(run.profile, 'write_auth') as write_auth, patch.object(run, 'real_bin', return_value='/test/native-codex'), patch.object(run, '_path_without_shim', return_value='/test/bin'), patch.object(run.os, 'execv') as native:
    run.exec_harness(cfg, codex, 'main', auth, ['exec', 'test'])
    write_auth.assert_called_once_with(codex, '/test/profile', auth)
    native.assert_called_once_with('/test/native-codex', ['codex', 'exec', 'test'])
    assert run.os.environ['CODEX_REFRESH_TOKEN_URL_OVERRIDE'].startswith(cfg['url'])
    assert run.os.environ['BROKER_ACTIVE'] == 'codex:main'
`;
  execFileSync("python3", ["-c", program], { cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
});

test("Medulla overlay creation is explicit; an existing overlay still updates without copying credentials", async (t) => {
  const dir = await temp(t);
  t.mock.method(os, "homedir", () => dir);
  const config = require("../lib/config");
  t.mock.method(config, "read", () => ({ url: "https://broker.example.test", key: "fake-admin-key" }));
  await fs.mkdir(path.join(dir, ".medulla"));
  // Load after the homedir mock: wrapper installation never touches the real home.
  const { install } = require("../lib/wrap");
  const installed = install("codex");
  assert.equal(installed.path, path.join(dir, ".local", "bin", "broker-cx"));
  const overlay = path.join(dir, ".medulla", "container");
  await assert.rejects(fs.stat(overlay), { code: "ENOENT" });
  install("codex", null, undefined, { container: true });
  const bundle = path.join(overlay, "bin", "broker-cx");
  const shim = await fs.readFile(path.join(overlay, "home", ".local", "bin", "codex"), "utf8");
  assert.match(shim, /exec \/usr\/local\/bin\/broker-cx/);
  assert.equal((await fs.stat(bundle)).mode & 0o777, 0o755);
  assert.equal(await readJson(path.join(overlay, "home", ".config", "broker", "config.json")), null);
  const program = "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); assert '__main__.py' in z.namelist(); assert 'BROKER_CONFIG' in z.read('broker/config.py').decode(); assert 'fake-admin-key' not in str([z.read(n) for n in z.namelist() if not n.endswith('/')])";
  execFileSync("python3", ["-c", program, bundle], { env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  await fs.unlink(bundle);
  install("codex");
  assert.equal((await fs.stat(bundle)).mode & 0o777, 0o755);
});

test("a fresh auto-picked retry skips a broken account, while an explicit account stays pinned", () => {
  const program = `
import sys
from unittest.mock import patch
sys.path.insert(0, 'lib/wrappers')
from broker import select, accounts, api, profile
from broker.providers import codex
cfg = {'accounts': {'codex': 'main'}}
good = dict(accounts.stub('main'), auth={'tokens': {'access_token': 'fake-main'}}, used=10)
bad = dict(accounts.stub('main'), error='needs re-auth')
backup = dict(accounts.stub('backup'), auth={'tokens': {'access_token': 'fake-backup'}}, used=20)
with patch.object(accounts, 'probe', side_effect=[good, bad, bad]), patch.object(api, 'list_accounts', return_value=['main','backup']), patch.object(accounts, 'probe_all', return_value=[backup,bad]), patch.object(profile, 'ensure'), patch.object(select, '_announce'):
    assert select.resolve(cfg, codex, None)[0] == 'main'
    assert select.resolve(cfg, codex, None)[0] == 'backup'
    try:
        select.resolve(cfg, codex, 'main')
    except SystemExit:
        pass
    else:
        raise AssertionError('explicit account must not silently switch')
`;
  execFileSync("python3", ["-c", program], { cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" }, stdio: "pipe" });
});
