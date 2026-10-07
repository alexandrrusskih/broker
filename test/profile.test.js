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
from broker import credentials, layout
from broker.providers import codex

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    canonical = root / '.codex'
    main, second = root / '.codex-main', root / '.codex-second'
    with patch.object(codex, 'CANONICAL_HOME', str(canonical)):
        assert not canonical.exists()
        credentials.write_auth(codex, str(main), {'synthetic': 'main'})
        assert canonical.is_dir()
        assert stat.S_IMODE(canonical.stat().st_mode) == 0o700
        assert not (canonical / 'auth.json').exists()
        # A future file name must be shared without editing a hard-coded list.
        (canonical / 'future-state.json').write_text('shared')
        credentials.write_auth(codex, str(second), {'synthetic': 'second'})
        layout.prepare(codex, str(main))
        layout.prepare(codex, str(second))
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
from broker import credentials, layout
from broker.providers import codex

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    canonical, account = root / '.codex', root / '.codex-main'
    account.mkdir()
    for suffix, value in [('', 'database'), ('-wal', 'checkpoint'), ('-shm', 'index')]:
        (account / ('thread_history_1.sqlite' + suffix)).write_text(value)
    (account / 'auth.json').write_text('private sentinel')
    with patch.object(codex, 'CANONICAL_HOME', str(canonical)):
        layout.prepare(codex, str(account))
        assert (account / 'thread_history_1.sqlite').is_symlink()
        for suffix, value in [('', 'database'), ('-wal', 'checkpoint'), ('-shm', 'index')]:
            assert (canonical / ('thread_history_1.sqlite' + suffix)).read_text() == value
        assert (account / 'auth.json').read_text() == 'private sentinel'
        assert not (canonical / 'auth.json').exists()
        # Existing copies remain a deliberate merge, never overwritten by prepare.
        (canonical / 'state_5.sqlite').write_text('canonical')
        (account / 'state_5.sqlite').write_text('account')
        layout.prepare(codex, str(account))
        assert (canonical / 'state_5.sqlite').read_text() == 'canonical'
        assert (account / 'state_5.sqlite').read_text() == 'account'
print('ok')
`;
  assert.equal(execFileSync("python3", ["-B", "-c", program], { cwd: root, encoding: "utf8" }).trim(), "ok");
});

test("Codex resume repairs dead indexed paths without changing missing sessions", () => {
  const program = `
import os, sqlite3, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'lib/wrappers')
from broker.providers import codex

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    canonical = root / '.codex'
    sessions = canonical / 'sessions' / '2026' / '09' / '07'
    sessions.mkdir(parents=True)
    db_dir = root / 'db'
    db_dir.mkdir()
    good = '01a07c48-6313-7db1-864d-75dedd239405'
    missing = '01a07c49-6313-7db1-864d-75dedd239405'
    filename = 'rollout-2026-09-07T16-31-53-' + good + '.jsonl'
    current = sessions / filename
    current.write_text('session')
    old = root / 'deleted-probe' / 'sessions' / '2026' / '09' / '07' / filename
    absent = root / 'deleted-probe' / 'sessions' / '2026' / '09' / '07' / ('rollout-2026-09-07T16-32-53-' + missing + '.jsonl')
    connection = sqlite3.connect(db_dir / 'state_5.sqlite')
    connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)')
    connection.executemany('INSERT INTO threads VALUES (?, ?)', [(good, str(old)), (missing, str(absent))])
    connection.commit()
    connection.close()
    with patch.object(codex, 'CANONICAL_HOME', str(canonical)):
        codex.harness_env({'CODEX_SQLITE_HOME': str(db_dir)})
    connection = sqlite3.connect(db_dir / 'state_5.sqlite')
    rows = dict(connection.execute('SELECT id, rollout_path FROM threads'))
    assert rows == {good: str(current), missing: str(absent)}
print('ok')
`;
  assert.equal(execFileSync("python3", ["-B", "-c", program], { cwd: root, encoding: "utf8" }).trim(), "ok");
});

// A run that must see none of your MCP servers, while keeping the login. agy is
// the only harness this applies to: its profile mirrors your whole home, and the
// mirror shares .gemini/config as one link — so the "isolated" MCP file inside a
// profile was literally yours, and writing it would edit the shared source.
test("a mirrored profile can own its MCP file while sharing the rest, and only when asked", () => {
  const program = `
import os, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'lib/wrappers')
from broker import layout, profile, tree
from broker.providers import agy

def old_algorithm(provider, path):
    """shared_entries exactly as it was with a single private path."""
    pairs, parts = [], provider.AUTH_NAME.split(os.sep)
    canonical, dst = provider.CANONICAL_HOME, path
    for depth, private in enumerate(parts):
        for name in tree._entries(canonical):
            if name != private:
                pairs.append((os.path.join(canonical, name), os.path.join(dst, name)))
        if depth == len(parts) - 1:
            break
        canonical, dst = os.path.join(canonical, private), os.path.join(dst, private)
    return pairs

with tempfile.TemporaryDirectory() as directory:
    home = Path(directory) / 'home'
    (home / '.gemini' / 'config').mkdir(parents=True)
    (home / '.gemini' / 'antigravity-cli').mkdir(parents=True)
    (home / '.gitconfig').write_text('shared')
    (home / '.gemini' / 'settings.json').write_text('shared')
    (home / '.gemini' / 'config' / 'mcp_config.json').write_text('{"mcpServers":{"yours":{}}}')
    (home / '.gemini' / 'config' / 'config.json').write_text('shared')
    (home / '.gemini' / 'antigravity-cli' / 'workspaces.json').write_text('shared')
    mcp = str(home / '.gemini' / 'config' / 'mcp_config.json')

    with patch.object(agy, 'CANONICAL_HOME', str(home)), \
         patch.object(agy, 'MCP_CONFIG', (mcp, 'json', 'mcpServers')):
        plain, isolated = Path(directory) / 'p-plain', Path(directory) / 'p-iso'

        # The default is every byte of the previous behaviour.
        assert sorted(profile.shared_entries(agy, str(plain))) == \
               sorted(old_algorithm(agy, str(plain))), 'default must not change'

        shared_now = {s for s, _ in profile.shared_entries(agy, str(plain))}
        assert str(home / '.gemini' / 'config') in shared_now, 'config is shared whole by default'

        shared_iso = {s for s, _ in profile.shared_entries(agy, str(isolated), isolate_mcp=True)}
        assert str(home / '.gemini' / 'config') not in shared_iso, 'config must be split'
        assert mcp not in shared_iso, 'the MCP file is the profile\\'s own'
        assert str(home / '.gemini' / 'config' / 'config.json') in shared_iso, \
               'its siblings stay shared'
        assert str(home / '.gitconfig') in shared_iso, 'and so does the rest of the home'

        # The mirror on disk: a link where it shared, a real directory where it split.
        layout.mirror(agy, str(plain))
        assert (plain / '.gemini' / 'config').is_symlink()

        layout.mirror(agy, str(isolated), isolate_mcp=True)
        assert not (isolated / '.gemini' / 'config').is_symlink(), 'split, not linked'
        assert (isolated / '.gemini' / 'config').is_dir()
        assert (isolated / '.gemini' / 'config' / 'config.json').is_symlink()
        assert not (isolated / '.gemini' / 'config' / 'mcp_config.json').exists(), \
               'the profile owns it, and nothing has written it yet'
        # Writing it here must not reach the shared source.
        (isolated / '.gemini' / 'config' / 'mcp_config.json').write_text('{"mcpServers":{}}')
        assert (home / '.gemini' / 'config' / 'mcp_config.json').read_text() == \
               '{"mcpServers":{"yours":{}}}', 'the shared file is untouched'
        # Credentials stay private, which is what the mirror was for.
        assert not (isolated / '.gemini' / 'antigravity-cli' / \
                    'antigravity-oauth-token').exists()
        assert (isolated / '.gemini' / 'antigravity-cli' / 'workspaces.json').is_symlink()

        # Turning it on again is idempotent, and a link the user aimed elsewhere stays.
        layout.mirror(agy, str(isolated), isolate_mcp=True)
        assert (isolated / '.gemini' / 'config' / 'mcp_config.json').read_text() == \
               '{"mcpServers":{}}'
print('ok')
`;
  assert.equal(execFileSync("python3", ["-B", "-c", program], { cwd: root, encoding: "utf8" }).trim(), "ok");
});
