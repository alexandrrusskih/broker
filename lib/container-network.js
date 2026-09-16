const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const YAML = require('yaml');
const { openPrompter } = require('./prompt');

const DEFAULT_IMAGE = 'python:3.12-slim';
const PROBE = fs.readFileSync(path.join(__dirname, 'container-probe.py'), 'utf8');

function command(program, args, options = {}) {
  try {
    return String(execFileSync(program, args, { encoding: 'utf8', timeout: 30000, stdio: 'pipe', ...options }) || '').trim();
  } catch (error) {
    throw new Error(`${program} ${args[0]} failed: ${String(error.stderr || error.code || 'command failed').trim().slice(0, 600)}`);
  }
}

function dockerTarget(run, env, home) {
  const context = env.DOCKER_CONTEXT || (!env.DOCKER_HOST ? run('docker', ['context', 'show']) : null);
  const endpoint = context
    ? JSON.parse(run('docker', ['context', 'inspect', context]))[0].Endpoints.docker.Host
    : env.DOCKER_HOST;
  if (!endpoint?.startsWith('unix://')) {
    throw new Error('container setup requires a local Docker daemon; run it on the Docker host (remote bind mounts are not local files)');
  }
  const socket = decodeURIComponent(new URL(endpoint).pathname);
  const root = path.resolve(env.COLIMA_HOME || path.join(home, '.colima'));
  const profile = path.basename(path.dirname(socket));
  const colima = path.basename(socket) === 'docker.sock' && path.dirname(path.dirname(socket)) === root
    && /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(profile)
    ? { profile, file: path.join(root, profile, 'colima.yaml') } : null;
  // Pin every probe to the inspected daemon, even if the active context changes.
  return { endpoint, colima };
}

function describe(result) {
  return ({ dns: 'broker hostname does not resolve inside Docker', tls: 'broker TLS certificate verification failed',
    connect: 'container cannot connect to the broker', config: 'container cannot read its client config',
    protocol: 'broker returned an unexpected response', empty: 'broker accepted the key, but no Codex accounts are seeded' })[result.kind]
    || `broker returned HTTP ${result.status} (check the client key/URL; redirects are not followed)`;
}

function dnsPlan(file, host, ip) {
  const original = fs.readFileSync(file, 'utf8');
  const document = YAML.parseDocument(original);
  if (document.errors.length) throw new Error('cannot parse Colima config; fix it manually before changing DNS');
  const dns = document.getIn(['network', 'dns']);
  if (dns && (!YAML.isSeq(dns) || dns.items.length)) {
    throw new Error('Colima has custom DNS resolvers; dnsHosts would be ignored. Existing DNS was left untouched');
  }
  document.setIn(['network', 'dnsHosts', host], ip);
  return { original, next: document.toString() };
}

function savePlan(file, plan) {
  if (fs.readFileSync(file, 'utf8') !== plan.original) throw new Error('Colima config changed during setup; rerun before applying DNS');
  const id = crypto.randomUUID();
  const backup = `${file}.broker-backup-${id}`;
  fs.copyFileSync(file, backup, fs.constants.COPYFILE_EXCL);
  const temp = `${file}.broker-${id}`;
  try {
    fs.writeFileSync(temp, plan.next, { mode: fs.statSync(file).mode & 0o777, flag: 'wx' });
    fs.renameSync(temp, file);
  } finally { fs.rmSync(temp, { force: true }); }
  return backup;
}

async function verifyContainer(options, deps = {}) {
  const run = deps.run || command;
  const env = deps.env || process.env;
  const log = deps.log || console.log;
  const target = dockerTarget(run, env, deps.home || os.homedir());
  const docker = (args, opts) => run('docker', ['--host', target.endpoint, ...args], opts);
  docker(['info', '--format', '{{.ServerVersion}}']);
  const image = options.image || DEFAULT_IMAGE;
  // Pull deliberately before mounting a credential. An existing local image is reused.
  try { docker(['image', 'inspect', image]); }
  catch { log(`Pulling connection-check image ${image}...`); docker(['pull', image], { timeout: 120000 }); }
  const probe = () => {
    const name = `broker-connection-${crypto.randomUUID()}`;
    try {
      const output = docker(['run', '--rm', '--pull', 'never', '--name', name, '-i', '--read-only',
        '--cap-drop=ALL', '--security-opt=no-new-privileges',
        '--mount', `type=bind,src=${options.file},dst=/run/broker-client.json,readonly`,
        '--entrypoint', 'python3', image, '-B', '-'], { input: PROBE, timeout: 30000 });
      return JSON.parse(output);
    } finally {
      // --rm handles normal exits; a timed-out docker client can leave its container alive.
      try { docker(['rm', '-f', name]); } catch { /* already removed */ }
    }
  };
  log(`Checking broker access from Docker (${image}, default network)...`);
  let result = probe();
  if (result.kind === 'ok') return { image, accounts: result.accounts, repaired: false };
  if (result.kind !== 'dns') throw new Error(`container setup incomplete: ${describe(result)}`);
  if (!target.colima) {
    throw new Error('container setup incomplete: broker DNS fails in Docker. Check Docker Desktop/VPN DNS settings; no network settings were changed');
  }
  const host = new URL(options.url).hostname.toLowerCase().replace(/\.$/, '');
  const state = JSON.parse(run('tailscale', ['status', '--json']));
  const node = [state.Self, ...Object.values(state.Peer || {})]
    .find(n => n?.DNSName?.toLowerCase().replace(/\.$/, '') === host);
  const ip = node?.TailscaleIPs?.find(a => net.isIPv4(a));
  if (state.BackendState !== 'Running' || !ip) {
    throw new Error('container setup incomplete: broker hostname is not a known node in the running Tailscale network; DNS was left unchanged');
  }
  const plan = dnsPlan(target.colima.file, host, ip);
  const running = docker(['ps', '--format', '{{.Names}}']);
  log(`DNS repair: ${host} -> ${ip} in Colima profile ${target.colima.profile}.`);
  log(`Colima must restart. Running containers: ${running || 'none'}.`);
  const ui = options.noAsk ? null : (deps.openPrompter || openPrompter)();
  let approved;
  try { approved = ui && /^y(es)?$/i.test(await ui.ask('Save this DNS mapping and restart Colima? [y/N] ') || ''); }
  finally { ui?.close(); }
  if (!approved) throw new Error('container setup incomplete: DNS repair/restart not approved; rerun broker setup --container interactively. No network settings changed');
  const backup = savePlan(target.colima.file, plan);
  log(`Colima config backup: ${backup}`);
  try { run('colima', ['restart', '--profile', target.colima.profile], { timeout: 180000, stdio: 'inherit' }); }
  catch (error) { throw new Error(`${error.message}. DNS config backup: ${backup}; finish starting Colima, then rerun setup`); }
  result = probe();
  if (result.kind !== 'ok') throw new Error(`container setup incomplete after restart: ${describe(result)}. Config backup: ${backup}`);
  return { image, accounts: result.accounts, repaired: true };
}

module.exports = { verifyContainer, dockerTarget, dnsPlan, savePlan, DEFAULT_IMAGE };
