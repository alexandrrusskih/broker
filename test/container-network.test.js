const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const YAML = require('yaml');
const { verifyContainer, dockerTarget, dnsPlan, savePlan } = require('../lib/container-network');

function fixture(t, results = [{ kind: 'ok', accounts: 2 }], overrides = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'broker-network-test-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const file = path.join(home, '.colima', 'work', 'colima.yaml');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, '# keep me\ncpu: 4\nnetwork:\n  dns: null\n  dnsHosts:\n    other.test: 192.0.2.10\n');
  const endpoint = `unix://${path.dirname(file)}/docker.sock`;
  const calls = [], logs = [];
  let prompts = 0;
  const deps = {
    home, env: {}, log: s => logs.push(s),
    openPrompter: () => ({ ask: async () => { prompts++; return 'yes'; }, close() {} }),
    run(program, args, options) {
      calls.push({ program, args, options });
      if (program === 'docker') {
        if (args[0] === 'context' && args[1] === 'show') return 'colima-work';
        if (args[0] === 'context' && args[1] === 'inspect') return JSON.stringify([{ Endpoints: { docker: { Host: endpoint } } }]);
        assert.deepEqual(args.slice(0, 2), ['--host', endpoint]);
        if (args[2] === 'run') return JSON.stringify(results.shift());
        if (args[2] === 'ps') return 'another-service';
        return '';
      }
      if (program === 'tailscale') return JSON.stringify({ BackendState: 'Running', Peer: { node: {
        DNSName: 'broker.tail.test.', TailscaleIPs: ['100.70.80.90']
      } } });
      if (program === 'colima') { assert.deepEqual(args, ['restart', '--profile', 'work']); return ''; }
      assert.fail(`unexpected program ${program}`);
    },
    ...overrides
  };
  return { deps, home, file, endpoint, calls, logs, prompts: () => prompts,
    options: { file: path.join(home, 'client.json'), url: 'https://broker.tail.test' } };
}

test('healthy setup checks authenticated access without DNS bypasses, secrets in argv or service changes', async t => {
  const f = fixture(t);
  const result = await verifyContainer(f.options, f.deps);
  assert.equal(result.accounts, 2);
  assert.equal(result.repaired, false);
  assert.equal(f.prompts(), 0);
  assert.ok(f.calls.every(c => c.program === 'docker'));
  const probe = f.calls.find(c => c.args[2] === 'run');
  assert.ok(probe.args.includes('python:3.12-slim'));
  assert.ok(probe.args.includes(`type=bind,src=${f.options.file},dst=/run/broker-client.json,readonly`));
  assert.ok(!probe.args.some(a => ['--dns', '--add-host', '--network', '-e', '--env'].includes(a)));
  assert.match(probe.options.input, /listAccounts/);
});

test('DNS repair preserves other config, backs up, restarts only the active profile and retests', async t => {
  const f = fixture(t, [{ kind: 'dns' }, { kind: 'ok', accounts: 2 }]);
  const before = fs.readFileSync(f.file, 'utf8');
  const result = await verifyContainer({ ...f.options, image: 'my-medulla:local' }, f.deps);
  assert.equal(result.repaired, true);
  assert.equal(f.prompts(), 1);
  const after = fs.readFileSync(f.file, 'utf8');
  assert.match(after, /# keep me/);
  assert.deepEqual(YAML.parse(after), { cpu: 4, network: { dns: null, dnsHosts: {
    'other.test': '192.0.2.10', 'broker.tail.test': '100.70.80.90'
  } } });
  const backups = fs.readdirSync(path.dirname(f.file)).filter(n => n.includes('.broker-backup-'));
  assert.equal(backups.length, 1);
  assert.equal(fs.readFileSync(path.join(path.dirname(f.file), backups[0]), 'utf8'), before);
  assert.equal(f.calls.filter(c => c.args[2] === 'run').length, 2);
  assert.ok(f.calls.filter(c => c.args[2] === 'run').every(c => c.args.includes('my-medulla:local')));
  assert.ok(f.logs.some(s => s.includes('another-service')));
});

test('no-ask and declined confirmation never write DNS or restart Colima', async t => {
  for (const noAsk of [true, false]) {
    const f = fixture(t, [{ kind: 'dns' }], { openPrompter: () => {
      assert.equal(noAsk, false, 'no-ask must not open a terminal');
      return { ask: async () => 'no', close() {} };
    } });
    const before = fs.readFileSync(f.file, 'utf8');
    await assert.rejects(verifyContainer({ ...f.options, noAsk }, f.deps), /not approved/);
    assert.equal(fs.readFileSync(f.file, 'utf8'), before);
    assert.deepEqual(fs.readdirSync(path.dirname(f.file)), ['colima.yaml']);
    assert.ok(!f.calls.some(c => c.program === 'colima'));
  }
});

test('TLS/auth/routing/empty-account errors never attempt a DNS repair', async t => {
  for (const kind of ['tls', 'http', 'connect', 'empty', 'protocol', 'config']) {
    const f = fixture(t, [{ kind, status: 401 }]);
    await assert.rejects(verifyContainer(f.options, f.deps), /container setup incomplete/);
    assert.ok(f.calls.every(c => c.program === 'docker'));
    assert.equal(f.prompts(), 0);
  }
});

test('failed post-restart test cannot report ready; backup remains available', async t => {
  const f = fixture(t, [{ kind: 'dns' }, { kind: 'http', status: 401 }]);
  await assert.rejects(verifyContainer(f.options, f.deps), /incomplete after restart.*401.*backup/);
  assert.equal(f.calls.filter(c => c.program === 'colima').length, 1);
});

test('custom DNS is never overridden and stale config cannot be overwritten', async t => {
  const f = fixture(t, [{ kind: 'dns' }]);
  fs.writeFileSync(f.file, 'network:\n  dns: [1.1.1.1]\n');
  await assert.rejects(verifyContainer(f.options, f.deps), /custom DNS/);
  assert.equal(f.prompts(), 0);
  assert.ok(!f.calls.some(c => c.program === 'colima'));
  fs.writeFileSync(f.file, 'network:\n  dns: []\n  dnsHosts: {}\n');
  const plan = dnsPlan(f.file, 'broker.tail.test', '100.70.80.90');
  fs.appendFileSync(f.file, '# another edit\n');
  assert.throws(() => savePlan(f.file, plan), /changed during setup/);
});

test('Docker Desktop is checked without invoking Colima; remote daemons are refused', async t => {
  const f = fixture(t, [{ kind: 'dns' }]);
  const run = f.deps.run;
  f.deps.env = { DOCKER_HOST: 'unix:///var/run/docker.sock' };
  f.deps.run = (program, args, opts) => run(program,
    args[0] === '--host' ? ['--host', f.endpoint, ...args.slice(2)] : args, opts);
  await assert.rejects(verifyContainer(f.options, f.deps), /Docker Desktop/);
  assert.ok(f.calls.every(c => c.program === 'docker'));
  assert.throws(() => dockerTarget(() => assert.fail('must honor DOCKER_HOST'),
    { DOCKER_HOST: 'ssh://remote' }, f.home), /local Docker daemon/);
});

test('Docker context precedence and custom Colima homes are respected', () => {
  const run = (program, args) => {
    assert.deepEqual(args, ['context', 'inspect', 'chosen']);
    return JSON.stringify([{ Endpoints: { docker: { Host: 'unix:///custom/colima/dev/docker.sock' } } }]);
  };
  const result = dockerTarget(run, { DOCKER_CONTEXT: 'chosen', DOCKER_HOST: 'ssh://ignored', COLIMA_HOME: '/custom/colima' }, '/unused');
  assert.deepEqual(result.colima, { profile: 'dev', file: '/custom/colima/dev/colima.yaml' });
});

test('unknown Tailscale host is not guessed or written to DNS', async t => {
  const f = fixture(t, [{ kind: 'dns' }]);
  await assert.rejects(verifyContainer({ ...f.options, url: 'https://unrelated.test' }, f.deps), /not a known node/);
  assert.equal(f.prompts(), 0);
});

test('Python probe classifies responses and never follows redirects or prints secrets', () => {
  execFileSync('python3', ['-B', path.join(__dirname, 'container_probe_test.py')], { stdio: 'pipe' });
});
