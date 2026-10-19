"""The harness's own files and directories, as the box sees them."""

import json
import os
import re
import shutil
import stat
import uuid

from .. import config
from ..out import warn
from . import http_mcp
from .paths import _bind, _empty_file, _mount, _paths, expand


def _trust(path, profile):
    """Answer the folder-trust question in the box's own copy of the config.

    A box exists to work in the directories it was given, and the person gave
    them: they are in its entry in boxes.json. Asking inside the box whether to
    trust one of them stops the harness on its first screen, before it reads a
    prompt — and the answer cannot even persist, because the file it would be
    written to is a copy made for this box alone.

    So it is answered here, in that copy, for those paths and nothing else. The
    host's file is not touched, and a directory the box was not given is still
    asked about.
    """
    targets = [target for _, target in _paths(profile, "rw")]
    if not targets:
        return
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        projects = data.setdefault("projects", {})
        for target in targets:
            projects.setdefault(target, {})["hasTrustDialogAccepted"] = True
        config.write_json(path, data)
    except (OSError, ValueError, AttributeError) as exc:
        warn("could not pre-answer the trust prompt for this box (%s)" % exc)


def own_home(provider, profile, env, name, mounted):
    """Mount the harness home. Appends to `mounted` what it mounted whole."""
    args = []
    # The harness's own directory: settings, MCP servers, agents, history.
    #
    # Directories are mounted; single FILES are copied in fresh instead. A bind
    # mount of a file pins one inode, and these files are rewritten atomically —
    # written beside, then renamed over. After the first such write the mount
    # points at an inode nothing links to any more, and inside the box the file
    # has simply vanished: claude reported ~/.claude.json missing and started
    # offering to restore it from a backup, while the host's copy was fine.
    http_config = None if profile.get("mcp") is False or provider.NAME == "codex" else http_mcp.stage(provider, env, name)
    http_config_mounted = False
    http_mode = "rw" if provider.NAME == "claude" else "ro"
    settings_mounted = set()
    for entry in getattr(provider, "BOX_HOME", ()):
        host = expand(entry)
        if not os.path.exists(host):
            continue
        if http_config and host == http_config[1]:
            if provider.NAME == "claude":
                _trust(http_config[0], profile)
            args += _bind(http_config[0], host, http_mode)
            http_config_mounted = True
            settings_mounted.add(host)
            continue
        if os.path.isdir(host):
            # A directory named in BOX_SETTINGS travels read-only, same as a
            # file does below. That is for a directory of CODE the host runs
            # too: opencode loads ~/.config/opencode/plugins by convention, so
            # a box able to write there could leave a line for the host's own
            # opencode to run at its next start.
            args += _mount(host, "ro" if entry in getattr(provider, "BOX_SETTINGS", ()) else "rw")
            mounted.append(host)
            continue
        copy = os.path.join(config.CONFIG_DIR, "box", "files", name.replace("/", "_"),
                            os.path.basename(host))
        try:
            os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
            shutil.copy2(host, copy)
        except OSError as exc:
            warn("could not stage %s for the box (%s) — it will be missing inside" % (host, exc))
            continue
        if provider.NAME == "claude" and os.path.basename(host) == ".claude.json":
            _trust(copy, profile)
        mode = "ro" if entry in getattr(provider, "BOX_SETTINGS", ()) else "rw"
        args += _bind(copy, host, mode)
        settings_mounted.add(host)
    # ...minus its credentials file. The token comes from the broker below.
    blank = None
    for entry in getattr(provider, "BOX_SECRETS", ()):
        host = expand(entry)
        if any(host.startswith(m + os.sep) for m in mounted):
            blank = blank or _empty_file()
            args += _bind(blank, host, "ro")
    args += _account_credentials(provider, env, name, mounted)
    return (args, http_config, http_config_mounted, http_mode, settings_mounted)


def _expiry(path):
    """When the token in this auth file runs out, as the string it is stored as.

    Comparable with itself and nothing else: ISO-8601 instants of one shape
    sort as time, and anything unreadable sorts before everything, which means
    "do not prefer this one".
    """
    try:
        with open(path) as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return ""
    token = loaded.get("token") if isinstance(loaded, dict) else None
    when = token.get("expiry") if isinstance(token, dict) else None
    return when if isinstance(when, str) else ""


def _carry_credentials_back(copy, source):
    """Fold a token the box refreshed back into the account's profile.

    The box gets a COPY, because the file is rewritten atomically and a bind
    mount holds the inode it was given. The cost was the other direction: a
    harness inside refreshes its own token, writes it to its copy, and that
    copy is replaced at the box's next start — so the work was lost and the
    profile went on handing out the older one. It is the same shape as the MCP
    token store, and the same answer: carry it home before replacing it.

    Only when the box's copy expires LATER. A copy that was never written is
    byte for byte the profile's own, and one the broker has since refreshed
    must not be pushed backwards by a box holding yesterday's.
    """
    if not os.path.exists(copy):
        return
    if _expiry(copy) <= _expiry(source):
        return
    try:
        shutil.copy2(copy, source)
        os.chmod(source, 0o600)  # it is a credential
    except OSError as exc:
        warn("could not carry the box's %s token back (%s)"
             % (os.path.basename(source), exc))
        return
    warn("carried a newer %s token back from the box" % os.path.basename(source))


def _account_credentials(provider, env, name, mounted):
    """The token the broker wrote, where a harness whose profile IS a home looks.

    Every account gets its own profile, and for a provider with a config-dir
    variable the box is handed that profile directly — codex gets its
    CODEX_HOME mounted, so the token the wrapper wrote is the token it reads.
    agy has no such variable: its profile is a whole HOME, and a box cannot be
    given a different one, so the box mounts the CANONICAL ~/.gemini instead.
    The wrapper writes the token into ~/.agy-<account>, nothing put it on the
    canonical path, and inside the box agy found no credentials at all and
    offered to sign in — on a machine whose account the broker was serving
    perfectly. Measured on a second machine, where the canonical file does not
    exist because nobody ever logged in there by hand.

    Copied in, not bound: the wrapper rewrites that file atomically, beside
    and then renamed over, and a bind mount holds the inode it was given. The
    box would be left pointing at an inode nothing links to any more — the
    reason the single-file branch above copies too. So the box gets the token
    as it stood when it started, which is the same freshness every other
    private copy has.
    """
    args = []
    if getattr(provider, "CREDENTIALS", "file") == "env":
        return args
    # Only when nothing else will mount the profile: a provider with its own
    # config-dir variable is already handled, by chosen_home.
    if getattr(provider, "HOME_ENV", None) != "HOME":
        return args
    # Not every harness keeps its credentials in a file of its own — opencode
    # has no AUTH_NAME at all, and a box may carry it as a companion.
    if not getattr(provider, "AUTH_NAME", None):
        return args
    from ..profile import profile_dir

    active = (env or {}).get("BROKER_ACTIVE") or ""
    account = active.split(":", 1)[1] if ":" in active else ""
    if not account:
        return args
    source = os.path.join(profile_dir(provider, account), provider.AUTH_NAME)
    host = os.path.join(expand("~"), provider.AUTH_NAME)
    if not os.path.exists(source) or os.path.realpath(source) == os.path.realpath(host):
        return args
    if not any(host.startswith(m + os.sep) for m in mounted):
        return args  # the home it lives in did not come into this box
    copy = os.path.join(config.CONFIG_DIR, "box", "files", name.replace("/", "_"),
                        "auth-" + os.path.basename(source))
    try:
        os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
        _carry_credentials_back(copy, source)
        shutil.copy2(source, copy)
        os.chmod(copy, 0o600)  # it is a credential
    except OSError as exc:
        warn("could not give the box its %s credentials (%s)" % (provider.NAME, exc))
        return args
    return _bind(copy, host)


def siblings(provider, canonical):
    """Every profile of this harness, not only the one this run uses."""
    args = []
    # Directories that every profile of this harness should reach, not only the
    # one this run uses: a harness can record a path through a profile it is no
    # longer using, and the file it names is shared anyway.
    shared = getattr(provider, "BOX_SHARED", ())
    if shared:
        import glob as globmodule

        for sibling in sorted(globmodule.glob(canonical + "-*")):
            for entry in shared:
                host = os.path.join(sibling, entry)
                if os.path.exists(host):
                    args += _bind(os.path.realpath(host), host)
    return args


def _uncopyable(directory, names):
    """What a profile copy must skip: sockets and FIFOs are live runtime
    plumbing, not files. Copying a socket fails outright (Errno 102) and
    copying a FIFO blocks on open — and a box has no use for the host
    daemon's socket anyway.
    """
    skip = []
    for entry in names:
        try:
            mode = os.lstat(os.path.join(directory, entry)).st_mode
        except OSError:
            skip.append(entry)
            continue
        if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
            skip.append(entry)
    return skip


def chosen_home(provider, env, name, remote, mounted, canonical):
    """The config directory this run was pointed at, and the name for it."""
    args = []
    # The harness's config directory, wherever this run was pointed at: a
    # per-account profile for a file-credentials provider, and for claude
    # whatever CLAUDE_CONFIG_DIR says — a terminal manager gives each agent its
    # own, with the hooks it reports its state through. Not mounting it left the
    # harness running fine and the manager blind to it.
    home_env = getattr(provider, "HOME_ENV", None)
    if home_env and home_env != "HOME" and env.get(home_env):
        host = os.path.abspath(os.path.expanduser(env[home_env]))
        if os.path.isdir(host) and host not in mounted:
            source = host
            if provider.NAME == "codex" and host != canonical and not remote:
                root = os.path.join(config.CONFIG_DIR, "box", "profiles",
                                    name.replace("/", "_"))
                os.makedirs(root, mode=0o700, exist_ok=True)
                source = os.path.join(root, uuid.uuid4().hex)
                shutil.copytree(host, source, symlinks=True, ignore=_uncopyable)
            args += _mount(source, target=host)
            args += ["-e", "%s=%s" % (home_env, host)]
    return args


# An absolute path named inside a settings file: quoted — so it may hold spaces
# — or bare. Every candidate is checked against the filesystem below, so being
# generous here costs nothing and missing a form costs the protection.
SCRIPTS = (re.compile(r"'(/[^']+)'"),
           re.compile(r'"(/[^"]+)"'),
           re.compile(r"(/[\w./+-]+)"))


def _named_scripts(sources, protected):
    """Every script the read-only settings tell the harness to run, read-only too.

    The settings files are read-only above, and that is not enough: a command
    inside one NAMES a script, and those sit in the harness's own directory —
    which a box gets writable, because that is where the harness keeps its
    state. So a box could rewrite one and the HOST harness would run it at its
    next session event, outside any container. Confirmed on this machine for
    ~/.claude/hooks/herdr-agent-state.sh and ~/.codex/herdr-agent-state.sh.

    `sources` is every settings file mounted read-only just above, including the
    per-account profile copies — not the provider's declared list, which leaves
    those out.

    The boundary is the directory each settings file lives in, and not the
    harness home: agy's home IS your home, so "under the home" would have meant
    every tool you own — including the MCP servers mounted by name further down,
    which docker then refuses as a duplicate mount point. A settings file that
    sits directly in the home is skipped for the same reason.
    """
    args = []
    seen = set(protected)
    # Compared as written, not resolved: a settings file names a path the way
    # the person typed it, and resolving one side only never matches.
    home = expand("~") + os.sep
    for host in sources:
        inside = os.path.dirname(host) + os.sep
        if inside == home or home.startswith(inside):
            continue
        try:
            with open(host, encoding="utf-8", errors="replace") as handle:
                # JSON holds a quoted path as \", and the quote is what delimits
                # a name with a space in it.
                text = handle.read().replace('\\"', '"')
        except OSError:
            continue
        for pattern in SCRIPTS:
            for found in pattern.findall(text):
                if found in seen or not found.startswith(inside):
                    continue
                if not os.path.isfile(found):
                    continue
                seen.add(found)
                args += _mount(os.path.realpath(found), "ro", found)
    return args


def _trust_toml(path, targets):
    """codex's trust entries, appended for paths not yet trusted.

    TOML cannot be rewritten the way claude's JSON is without a writer the
    engine does not carry — but it needs none: `[projects."path"]` is a table,
    and appending a table that does not exist yet is valid TOML however the
    file is laid out. What exists already is found by scanning for the table
    header rather than by parsing: the engine must load on a python without
    tomllib (see test/installer.test.js), and a table that exists must never
    be appended twice — a duplicate table definition makes the whole file
    unparseable and takes codex down with it. A path the host has already
    decided on, either way, keeps its decision. Returns True when anything
    was added.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        missing = [t for t in targets
                   if not re.search(r'^\[projects\."%s"\]' % re.escape(t), text, re.M)]
        if not missing:
            return False
        with open(path, "a", encoding="utf-8") as handle:
            for target in missing:
                handle.write('\n[projects."%s"]\ntrust_level = "trusted"\n' % target)
    except OSError as exc:
        warn("could not pre-answer codex folder trust for this box (%s)" % exc)
        return False
    return True


def _trusted_config(provider, profile, name):
    """A staged copy of codex's config.toml with the box's folders trusted.

    codex asks folder trust on its first screen, and inside a box the answer
    cannot be saved: config.toml travels read-only (BOX_SETTINGS), the write
    dies with -32603, and the session never starts. The folders are the box's
    own rw list, given by the same person the question is meant for — so the
    answer is written into the copy the box is mounted, and the host's file
    is not touched. None when there is nothing to add: the real file will do.
    """
    if provider.NAME != "codex":
        return None
    targets = [target for _, target in _paths(profile, "rw")]
    if not targets:
        return None
    host = None
    for entry in getattr(provider, "BOX_SETTINGS", ()):
        candidate = expand(entry)
        if os.path.basename(candidate) == "config.toml" and os.path.isfile(candidate):
            host = candidate
            break
    if not host:
        return None
    copy = os.path.join(config.CONFIG_DIR, "box", "files", name.replace("/", "_"),
                        "config.toml")
    try:
        os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
        shutil.copy2(host, copy)
    except OSError as exc:
        warn("could not stage config.toml to trust the box's folders (%s)" % exc)
        return None
    if not _trust_toml(copy, targets):
        return None
    return copy


def settings(provider, profile, env, name, canonical, http_config, settings_mounted):
    """What the host decided and the box may read but not rewrite."""
    args = []
    # Host settings stay read-only. Runtime state in the same home stays writable.
    config_target = http_config[1] if http_config else None
    # Where the staged MCP copy will be mounted, compared by what it RESOLVES to.
    # The staged copy goes to the real path of the config this run was pointed
    # at; for a harness whose per-account profile is a mirror, that profile's
    # file is a symlink to the canonical one, so the two names differ while the
    # target is the same file. Comparing the names only, this mounted the
    # canonical config twice, and docker refused the container outright:
    # "Duplicate mount point: ~/.gemini/config/mcp_config.json". agy could not
    # start in a box at all on a real run — only on a passthrough like
    # --version, which picks no account and so has no profile.
    staged = set()
    if http_config:
        staged = {os.path.realpath(http_config[1]), os.path.realpath(http_config[2])}
    protected = set(settings_mounted)
    sources = []
    # codex's folder-trust answer rides in a staged copy of config.toml, and
    # every config.toml mount below serves that copy instead of the host's
    # file — read-only either way, so the box still cannot rewrite what the
    # host decided. See _trusted_config above.
    trusted = _trusted_config(provider, profile, name)
    for entry in getattr(provider, "BOX_SETTINGS", ()):
        host = expand(entry)
        if os.path.isfile(host):
            sources.append(host)
        if (os.path.isfile(host) and os.path.realpath(host) not in staged
                and host not in settings_mounted):
            source = trusted if trusted and os.path.basename(host) == "config.toml" \
                else os.path.realpath(host)
            args += _mount(source, "ro", host)
            protected.add(host)
    if provider.NAME == "codex" and env.get("CODEX_HOME"):
        profile_home = os.path.abspath(expand(env["CODEX_HOME"]))
        if profile_home != canonical:
            for filename in ("config.toml", "hooks.json"):
                host = os.path.join(profile_home, filename)
                if os.path.isfile(host):
                    source = trusted if trusted and filename == "config.toml" \
                        else os.path.realpath(host)
                    args += _mount(source, "ro", host)
                    protected.add(host)
                    sources.append(host)
    if config_target:
        protected.add(config_target)
    args += _named_scripts(sources, protected)
    return args
