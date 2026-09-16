const test = require('node:test');
const assert = require('node:assert/strict');
const { prepareDependencies } = require('../lib/dependencies');

const missing = () => { const error = new Error('missing yaml'); error.code = 'MODULE_NOT_FOUND'; throw error; };

test('runtime dependency bootstrap is a no-op when already installed', () => {
  prepareDependencies({ resolve: () => '/installed/yaml', run: () => assert.fail('no installation needed') });
});

test('fresh linked checkout bootstraps via npm or Bun without hooks or global writes', () => {
  for (const available of ['npm', 'bun']) {
    let installed = false;
    const pkg = '/test/source with spaces';
    prepareDependencies({ pkg, log() {}, resolve: () => installed ? '/installed/yaml' : missing(),
      run(tool, args, options) {
        if (args[0] === '--version') return { status: tool === available ? 0 : null };
        assert.equal(tool, available);
        assert.equal(options.cwd, pkg);
        assert.ok(args.includes('--ignore-scripts'));
        assert.ok(args.includes(pkg));
        if (tool === 'npm') assert.ok(args.includes('--global=false'));
        installed = true;
        return { status: 0 };
      }
    });
    assert.equal(installed, true);
  }
});

test('dependency install failure cannot silently pass setup', () => {
  assert.throws(() => prepareDependencies({ resolve: missing, log() {},
    run: (_tool, args) => ({ status: args[0] === '--version' ? 0 : 1 })
  }), /could not install/);
  assert.throws(() => prepareDependencies({ resolve: missing, log() {}, run: () => ({ status: null }) }), /npm or bun/);
});
