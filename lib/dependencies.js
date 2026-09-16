const path = require('node:path');
const { spawnSync } = require('node:child_process');

// Older installers/upgraders link a fresh checkout without installing its deps.
// Bootstrap at setup, so the first upgrade works too, not just the next one.
function prepareDependencies(deps = {}) {
  const resolve = deps.resolve || (() => require.resolve('yaml'));
  try { resolve(); return; }
  catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  const run = deps.run || spawnSync;
  const pkg = deps.pkg || path.resolve(__dirname, '..');
  for (const tool of ['npm', 'bun']) {
    if (run(tool, ['--version'], { stdio: 'ignore' }).status !== 0) continue;
    (deps.log || console.log)('Installing broker runtime dependencies...');
    const args = tool === 'npm'
      ? ['install', '--prefix', pkg, '--global=false', '--ignore-scripts', '--no-audit', '--no-fund']
      : ['install', '--cwd', pkg, '--ignore-scripts'];
    if (run(tool, args, { cwd: pkg, stdio: 'inherit' }).status !== 0) {
      throw new Error('could not install broker runtime dependencies; fix the package manager/network and rerun setup');
    }
    resolve();
    return;
  }
  throw new Error('npm or bun is required to install broker runtime dependencies');
}

module.exports = { prepareDependencies };
