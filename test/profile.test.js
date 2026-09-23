const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const root = path.join(__dirname, "..");

test("fresh Codex profiles create their shared home without sharing credentials", () => {
  const program = `
import json, os, stat, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'lib/wrappers')
from broker import profile
from broker.providers import codex

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    canonical = root / '.codex'
    main, second = root / '.codex-main', root / '.codex-second'
    with patch.object(codex, 'CANONICAL_HOME', str(canonical)):
        assert not canonical.exists()
        profile.write_auth(codex, str(main), {'synthetic': 'main'})
        assert canonical.is_dir()
        assert stat.S_IMODE(canonical.stat().st_mode) == 0o700
        assert not (canonical / 'auth.json').exists()
        # A future file name must be shared without editing a hard-coded list.
        (canonical / 'future-state.json').write_text('shared')
        profile.write_auth(codex, str(second), {'synthetic': 'second'})
        profile.prepare(codex, str(main))
        profile.prepare(codex, str(second))
        for folder, expected in [(main, 'main'), (second, 'second')]:
            assert (folder / 'future-state.json').is_symlink()
            assert (folder / 'future-state.json').read_text() == 'shared'
            assert not (folder / 'auth.json').is_symlink()
            assert json.loads((folder / 'auth.json').read_text()) == {'synthetic': expected}
            assert stat.S_IMODE((folder / 'auth.json').stat().st_mode) == 0o600
        assert not (canonical / 'auth.json').exists()
print('ok')
`;
  assert.equal(execFileSync("python3", ["-B", "-c", program], { cwd: root, encoding: "utf8" }).trim(), "ok");
});

test("pre-existing profile databases are promoted before linking into a missing canonical home", () => {
  const program = `
import os, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'lib/wrappers')
from broker import profile
from broker.providers import codex

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    canonical, account = root / '.codex', root / '.codex-main'
    account.mkdir()
    for suffix, value in [('', 'database'), ('-wal', 'checkpoint'), ('-shm', 'index')]:
        (account / ('thread_history_1.sqlite' + suffix)).write_text(value)
    (account / 'auth.json').write_text('private sentinel')
    with patch.object(codex, 'CANONICAL_HOME', str(canonical)):
        profile.prepare(codex, str(account))
        assert (account / 'thread_history_1.sqlite').is_symlink()
        for suffix, value in [('', 'database'), ('-wal', 'checkpoint'), ('-shm', 'index')]:
            assert (canonical / ('thread_history_1.sqlite' + suffix)).read_text() == value
        assert (account / 'auth.json').read_text() == 'private sentinel'
        assert not (canonical / 'auth.json').exists()
        # Existing copies remain a deliberate merge, never overwritten by prepare.
        (canonical / 'state_5.sqlite').write_text('canonical')
        (account / 'state_5.sqlite').write_text('account')
        profile.prepare(codex, str(account))
        assert (canonical / 'state_5.sqlite').read_text() == 'canonical'
        assert (account / 'state_5.sqlite').read_text() == 'account'
print('ok')
`;
  assert.equal(execFileSync("python3", ["-B", "-c", program], { cwd: root, encoding: "utf8" }).trim(), "ok");
});
