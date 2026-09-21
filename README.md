# @pbl/broker

Centralized OAuth **token broker** — the *sole refresh authority* for
`codex` / `claude` / `agy` across CI runners and dev machines. It owns the
rotating refresh token, refreshes it centrally under a lock, and hands out fresh
access tokens — so no two clients ever race and burn each other's refresh token
(`refresh_token_reused`).

The broker can run **self-hosted** (one HTTP server + private JSON files) or as
Cloud Functions in a **dedicated Firebase project**. The API and refresh logic
are shared; clients only need a URL and a key. For a private Tailscale endpoint,
system LaunchDaemon and remote Docker clients, see [self-hosted setup](docs/self-hosted.md).

In the Firebase deployment, refresh
tokens live in Firestore (Google-managed encryption at rest, no customer-managed
KMS). Deny-all client rules isolate the database; only the function's Admin SDK
accesses it. A Secret Manager value protects the atomic one-time bootstrap.

## Install

git is the only source. The npm registry copy trails this repo, so installing
from it would put an older CLI — and an older `broker-cx` — over a working setup.

```sh
bash install.sh          # installs CLI; optionally prompts for a client connection
bash install.sh --server # opt-in macOS system daemon, suitable for SSH/headless use
```

Or by hand, from a checkout:

```sh
bun install -g .  # or: npm install -g .
```

An existing client connection is left untouched. A new client can enter its URL
and key interactively (the key is hidden), or use
`bash install.sh --client-config /path/to/client.json --no-ask`. A client install
does not create a daemon or replace the native Codex command; installing its shim
is the explicit `broker codex install` step.

`broker upgrade` updates the CLI and already-installed wrappers; `--all` also
updates the native harnesses. **Only `broker upgrade --server`** additionally
updates/restarts an installed self-hosted daemon. Firebase deployment is unchanged.

For **Medulla in Docker**, run this once on each Docker host (Docker Desktop or
Colima):

```sh
broker setup --container
# Or import a securely delivered CLIENT config directly:
broker setup --container --client-config /path/to/client.json
```

This installs the container Codex shim and a private client config in Medulla's
host overlay, then verifies authenticated access from a temporary container on
the default Docker network. It does not change the host's Codex setup.
An existing overlay keeps its connection; otherwise the saved host client is
used. On a managed broker server, a loopback connection is replaced by that
same server's external URL from `client.json`. Use `--url` to explicitly choose
a container-reachable endpoint. Container DNS/routing must reach it; ordinary
`docker run` does not mount Medulla's overlay automatically. For a known
Tailscale hostname that fails DNS on Colima, setup offers a single-host DNS
mapping and asks before restarting that profile. `--no-ask` never restarts
anything: an unresolved connection exits nonzero, not "ready". Docker Desktop
network settings are never edited. The probe uses `python:3.12-slim` (pulled if
missing); `--image <trusted-image-with-python3>` checks another image instead.

For an unpublished branch use `bash install.sh --from /path/to/checkout`
(add `--server` for the server), and `broker upgrade --from /path/to/checkout`
(also add `--server` to update that daemon). Without `--from`, both commands use
the upstream default branch, not the branch of the current directory.

## Use (zero-manual)

```sh
# 1) deploy to a dedicated Firebase project (functions + deny-all rules + key)
broker deploy --project my-broker-project --dedicated-project

# 2) log in to a provider as usual, then hand the broker its refresh token
codex login                # or: claude /login, agy
broker seed codex

# 3) use the wrapper instead of the bare CLI — it pulls auth from the broker
broker codex install       # 'broker-cx' + shim: plain 'codex' goes through the broker too
broker-cx exec "..."              # codex via broker; no rotation race
```

In CI, fetch a fresh auth file from the broker before running the tool. The
auth file now carries an **opaque handle**, not the real refresh token, so codex
must be told to route its own mid-session refresh/logout back through the broker —
otherwise, the moment codex self-refreshes, it would POST the handle to OpenAI and
fail. Export both override URLs with the same account (`broker-cx` does this for you; bare
CI codex must do it explicitly):

```sh
ACCOUNT="${CODEX_ACCOUNT:-account-a}"
install -d -m 700 ~/.codex
( umask 077; broker get codex --account "$ACCOUNT" --format authjson > ~/.codex/auth.json )
# Route codex's refresh through the broker ONLY when it handed us a HANDLE (64-hex).
# While the rollout flag is off the broker returns the REAL token, and codex must keep
# refreshing against OpenAI — setting the overrides then would POST the real token to
# /oauthRefresh and 401. This mirrors what `broker-cx` does automatically; do not export the
# overrides unconditionally.
RT=$(jq -r '.tokens.refresh_token // ""' ~/.codex/auth.json)
if printf '%s' "$RT" | grep -qE '^[0-9a-f]{64}$'; then
  BROKER_URL=$(jq -er '.url' ~/.config/broker/config.json)
  export CODEX_REFRESH_TOKEN_URL_OVERRIDE="$BROKER_URL/oauthRefresh?provider=codex&account=$ACCOUNT"
  export CODEX_REVOKE_TOKEN_URL_OVERRIDE="$BROKER_URL/oauthRevoke?provider=codex&account=$ACCOUNT"
fi
```

## Commands

| Command | What |
|---|---|
| `broker setup [--url <url> \| --client-config <file>] [--no-ask]` | Connect a new client without overwriting an existing connection. |
| `broker setup --container [--client-config <file>] [--url <url>] [--image <image>] [--no-ask]` | Prepare Medulla's overlay and verify container access; Colima DNS repair/restart requires confirmation. |
| `broker server install [--url <url>]` | Install/update a macOS system LaunchDaemon. |
| `broker server status\|restart\|stop` | Manage the system daemon. |
| `broker upgrade [--all] [--server] [--from <checkout>]` | Update CLI/wrappers, optionally harnesses and/or the managed server. |
| `broker deploy --project <id> --dedicated-project [--alert-webhook <url>]` | Secure deploy + bootstrap + save config. |
| `broker seed <codex\|claude\|agy>` | Give the broker a freshly-logged-in refresh token. |
| `broker get <provider> [--format authjson\|raw]` | Fetch a fresh token (scripts/CI). |
| `broker <provider> install` | Install a `broker-cx`/`broker-cl`/`broker-agy` wrapper. |
| `broker accounts <provider>` | List the accounts seeded for a provider. |
| `broker forget <provider> --account <name> --yes` | Delete an account and its token from the broker. |
| `broker set-default <account>` | Set the default account for commands and wrappers. |
| `broker config [--url <url>] [--key <key>]` | Show/set local config. |

## `--box`: give the harness everything, inside a container

The point is not sandboxing for its own sake. It is being able to say yes to
every permission a harness asks for — write files, run commands, install things —
while the blast radius stays the directories you named.

```sh
broker box build                 # the image: claude, codex, agy, gh, glab, node, bun, docker
claude --box work                # interactive, with only what 'work' lists
claude --box work --resume abc   # past sessions are still there
codex --box work exec "..."      # same box, other harness
```

Boxes live in `~/.config/broker/boxes.json`, which is yours to edit and is never
rewritten by the broker (comments are fine):

```jsonc
{
  "work": {
    "rw": ["~/Projects/example"],
    "ro": ["~/Projects/reference"]
  }
}
```

A box that needs more than the base image brings its own Dockerfile:

```jsonc
"infra": {
  "rw": ["~/Projects/example-infra"],
  "dockerfile": "~/Projects/example-infra/.box/Dockerfile"
}
```

It starts `FROM broker-box`, and `broker box build` builds the base and then
every box extending it, tagging this one `broker-box-infra`. Keeping project
tools out of the base is the point: a rust toolchain for one project and
browsers for another would quadruple an image every box shares. See
`box/Dockerfile.example`.

A box also decides what flags the harness starts with — including how much it is
trusted inside:

```jsonc
"work": {
  "rw": ["~/Projects/example"],
  "args": { "claude": ["--some-flag"], "codex": ["--another"] }
}
```

The broker does not decide that for you, and nothing is implied by a box
existing: an empty `args` means the harness behaves exactly as it does outside.
A plain list applies to every harness; flags you type yourself win, because the
box's come first.

Two decisions are worth knowing about:

**Paths match the host exactly.** `~/Projects/example` is mounted at
`~/Projects/example`, and `$HOME` is your `$HOME`. Harnesses key session history
off the absolute path of the working directory, so a project remapped to
`/workspace` would lose every past session and every `--resume`.

**The harness's own directory comes along, whole.** `~/.claude` for claude,
`~/.codex` for codex, `~/.gemini` for agy: settings, MCP servers, agents,
commands, history. Listing
parts of it would silently drop whatever the next release adds. The one exception
is the credentials file, which is covered by an empty one — the token arrives
from the broker in the environment, and a copy on disk inside the box is a copy
that can leave it.

Everything after a bare `--` reaches the harness untouched, so a prompt that
mentions `--box` is a prompt.

### What a box shares, and what it keeps

Sessions are shared; databases are not.

The harness's own directory comes along on its own — settings, MCP servers,
agents, history. Inside that directory the split is:

| | |
|---|---|
| session files (`jsonl`) | **shared** — written straight into the real pile on the host |
| databases (`*.sqlite`) | **private** — a copy per box, made fresh at every start |
| journals (`-wal`, `-shm`) | **private**, cloned and mounted alongside their database |

The databases cannot be shared, because SQLite's locks do not cross the
container boundary: a box holding an exclusive lock is invisible to the harness
outside, both write at once, and the file tears. Measured, then seen repeatedly
in one afternoon as "database disk image is malformed" and "wrong # of entries
in index".

The journals are part of that and are easy to miss. SQLite keeps them BESIDE
the database, and the newest pages live in `-wal` until a checkpoint folds them
in. Cloning the database alone leaves the journals coming from the directory
mount underneath — so a box writes its pages into its own copy and its journal
into everyone's. They are cloned too, including when the host has no journal
yet, since SQLite would otherwise create one in the shared directory the moment
it opens the database.

Nothing is lost by working on a copy, because the work itself is in the session
file, which is shared. On the way out the box asks the harness to read that
session back into the history out here (see `BOX_SYNC`), in the background —
folding is a full scan of every session the harness has, and waiting for it held
the prompt for seconds.

One consequence worth knowing: a thread started inside a box is recorded in the
box's copy of the database, and that record dies with the copy. The session
file survives, so `resume <id>` opens it and registers the thread out here — the
line the box prints on the way out is exactly that command. Until it is opened
once, the thread will not appear in the picker.

Registering it automatically on the way out was tried and taken back out.
Archiving the session and unarchiving it does the job, but between those two
commands the session IS archived — and a box starting in that instant clones a
database that says so, then refuses to reopen its own session over a thread the
host considers perfectly live. The session file is the record; one `resume` is
the price of having it listed.

### When a box dies badly

A harness takes the terminal over completely: alternate screen, mouse
reporting, and the kitty keyboard protocol, in which Enter arrives as `27;3u`
and an arrow as `1:1A`. On a normal exit it undoes all of that.

Killed outright it undoes none of it, and the pane is left answering the
keyboard in a language the shell underneath does not speak — typing into it
produces `zsh: command not found: 1:1A`. So the undoing lives out here, in the
thing that outlives the container: the terminal's settings are captured before
the run and restored afterwards whatever happened — a clean exit, a crash,
`docker stop` from another window, SIGTERM.

SIGKILL cannot be caught. For that one:

```sh
broker box repair    # run it in the pane that went strange
```

### ssh from a box

Mounting `~/.ssh` hands a box every key on the machine — GitHub, the cloud VMs,
whatever else is in there — when what it usually needs is one host. So keys are
named, one at a time:

```jsonc
{
  "work": {
    "rw": ["~/Projects/example"],
    "ssh": { "hosts": { "10.0.0.5": "~/.ssh/box_work" } }
  }
}
```

Only that key is mounted, at its own path. The others are not forbidden — they
are absent, so nothing inside can use them however it is asked to. The generated
config pins the host to its key with `IdentitiesOnly`, because the tools that
need this call plain `ssh <host>` with no `-i` of their own, and `known_hosts`
is built from your own, filtered to the hosts the box actually uses.

Your `~/.ssh/config` does not come along either, so a name that is not an
address needs spelling out — otherwise it resolves on the host and to nothing
inside the box:

```jsonc
"ssh": {
  "hosts": {
    "10.0.0.5": "~/.ssh/box_work",
    "gateway": { "key": "~/.ssh/box_gw", "hostname": "100.64.0.7", "user": "ops", "port": 2222 }
  }
}
```

`hostname`, `user`, `port` and `proxyjump` are written in ssh's own spelling;
anything else is passed through as given.

A box with no `ssh` section gets no ssh material at all.

### MCP inside a box

MCP servers keep working, including the ones that could never run in a container:
some of them are native macOS binaries, and the image has nothing to run them
with. So the server stays on the host and only its stdio crosses the
boundary — a listener on loopback here, a stand-in mounted **at the server's own
command path** there. The harness's config is not rewritten: it already points
at that path.

Servers reached over http need none of this and are left alone.

A loopback port is reachable by every process on the machine, so the first line
a client sends is a secret generated per listener and readable only by you
(`~/.config/broker/box/`). A connection that does not match is closed before any
server is spawned.

A bridged server also inherits the environment of whatever raised it, and then
outlives that shell. agentbus is the clear case: it takes its bus identity and
workspace from the Herdr pane it was started in (`ws_slug` comes from the Herdr
workspace, nothing else), so a listener raised in one pane and reused from
another would post to the bus as the wrong agent. A listener therefore belongs
to the identity that raised it; a different pane or workspace gets its own. Add
`"identity_env": ["SOME_VAR"]` under a server in a box if it keys on something
else.

Since the server runs on the host, it answers with the host's view of the world —
which is usually right (agentbus keeps your identity on the bus) and sometimes
not (a code-memory server would answer about the whole machine). So a box can say
what a server should see:

```jsonc
{
  "work": {
    "rw": ["~/Projects/example"],
    "mcp": {
      "codebase-memory": {
        "env": {
          "CBM_ALLOWED_ROOT": "~/Projects/example",
          "CBM_CACHE_DIR": "~/.cache/code-memory/example"
        }
      }
    }
  }
}
```

Any variable the server reads can go in there, `~` included. A value that is an
existing path is resolved through symlinks first — writing `~/Projects/foo` where
that is a link would otherwise key a code-memory database under a second name and
reindex the project from scratch. Values that are not paths are left alone.

The cache usually needs no box of its own: the server keys a database per
project already, so one store holds them all, and it lives on the host — the box
never touches those files. Split it only if you want the projects' databases in
separate directories.

`CBM_ALLOWED_ROOT` defaults to the box's first `rw` path even without that — as
the **physical** path, because that server keys its per-project database off the
path it is given, and the symlinked spelling would start a second database and
reindex the project from scratch. Set `"mcp": false` to bridge nothing.

For the same reason a project that is a symlink (`~/Projects/foo` →
`/Volumes/.../foo`) is mounted under **both** names. A server running on the host
resolves symlinks and answers with the physical path; without the second mount
the harness inside the box could not open a single file it named.

### Logging into an MCP server that wants OAuth

Some servers are reached over https and ask you to sign in (an error tracker, a
ticket tracker, anything with accounts of its own). Two things decide whether
that login survives, and both bite inside a box.

**Log in on the host, not in a box.** The flow opens a browser and waits on a
callback at `http://127.0.0.1:<port>`. A container has no browser, and the port
it listens on is not the port your browser would reach. Signing in on the host
is enough, because the tokens are shared — see below.

**Keep the tokens in a file.** codex stores them in the system keyring by
default, and a container has none: the callback arrives, the login succeeds, and
then there is nowhere to save it. In `~/.codex/config.toml`:

```toml
mcp_oauth_credentials_store = "file"
```

It is read at startup, so a box already running keeps the old setting.

**The tokens themselves are shared, deliberately.** They are credentials for
OTHER services, and those services are the same whichever account the broker
picks — so `~/.codex/.credentials.json` and the locks beside it are shared by
every profile, and a box gets the real file rather than an empty stand-in. One
login, and it holds for every account and inside every box.

The same applies to claude: `~/.claude/.credentials.json` holds `mcpOAuth`, not
the account's token — that one arrives in the environment and is never written
to disk — so it travels into a box like the rest of the directory.

A server that speaks stdio needs none of this: it runs on the host as you, and a
box reaches it through the bridge above.

## Accounts

Tokens are isolated per account in the broker, and every command takes
`--account <name>`.

The account is resolved per run: `$CODEX_ACCOUNT` / `$CLAUDE_ACCOUNT` /
`$AGY_ACCOUNT`, then `$BROKER_ACCOUNT`, then the default from
`broker set-default` — and with none of those set, the wrapper picks whichever
account has the most headroom left.

claude needs no profile: it takes its token from the environment, so `~/.claude`
stays one directory for every account — MCP servers, projects and history are
shared without a single symlink. codex and agy read a credentials *file*, so each
of their accounts gets its own profile directory (`~/.codex-<account>`,
`~/.agy-<account>`) and parallel runs never share one auth file.

## broker-cx: pick the account with room left

`broker-cx` chooses an account one of three ways: you name it, **your own account still
has room**, or it falls back to whoever has the most headroom left.

The sticky default is the point: an account should be used from as few places as
possible, because every extra machine on it is another IP against the same
subscription. So while your account (`broker set-default <name>`) has at least
20% of its window left, `broker-cx` stays there and **does not probe the others at all**
— a probe carries that account's own token, which would light it up from here
too. Below the threshold it looks for room elsewhere. Set `min_headroom` in the
broker config to move the line.

```sh
broker-cx                    # auto-pick, then run codex
broker-cx exec "..."         # same, arguments pass straight through
broker-cx account            # table: plan, % used, when it resets (broker-cx list is the same)
broker-cx account account-a  # run this one, whatever the default is
broker-cx auth account-b     # log a new account in and hand it to the broker, in one go
broker-cx login / logout / update   # pass straight through to codex, no account picked
broker-cx delete-auth account-b # forget it again — asks for the name first
broker-cx refresh            # create/link a profile for every seeded account
broker-cx version            # what is installed, and whether broker-cx is behind the repo
broker-cx upgrade            # pull the newest broker + wrapper from git, then update codex
```

```
   ACCOUNT  EMAIL             PLAN  USED  WINDOW  RESETS IN  STATUS
*  account-a  user-a@example.com  pro   98%   7d      18h 05m    ok
   account-b  user-b@example.com  pro   100%  7d      13h 27m    limit reached
```

The limits come from the same account snapshot codex shows under `/status`, and
reading them costs no quota — so every run reads them fresh, in parallel. That
is a second or two on the auto-pick path; naming an account skips it entirely.

Each account gets its own `$CODEX_HOME` — `~/.codex-<account>`, with no account
inheriting `~/.codex` by being special; it stays put as the shared original. The
shared parts — runtime, `config.toml`, `hooks.json`, plugins, skills, rules,
prompts, sessions — are symlinked back to `~/.codex`, so switching accounts
never forks your settings or history — the model cache included, since a stale
one only costs a refetch. An explicit `$CODEX_HOME` still wins.

A bare `broker-cx` already creates and re-links the profiles it sees on the way past —
it is filesystem work measured in fractions of a millisecond, so it is not worth
a separate step. `broker-cx refresh` is the explicit version: it also installs each
account's auth file, so every profile works straight away, including under a
bare `codex` with an explicit `CODEX_HOME`. It also points out profile
directories left behind by accounts the broker no longer knows, without touching
them.

`broker-cx auth <name>` is the whole onboarding of an extra account: it creates
`~/.codex-<name>` with its symlinks, runs `codex login` inside it (device-code
flow when it sees an SSH session, since a localhost redirect cannot reach a
browser on the other end), seeds the refresh token to the broker and reports
what the account has left. `--device` / `--browser` force either flow.

## broker-agy: the same, for agy

agy has no config-dir variable — it reads `$HOME/.gemini` and nothing else — so a
per-account profile has to be a whole home directory. `~/.agy-<account>` is built
as a mirror of yours: every entry is a symlink back to the real home, and only
the directories on the way down to `.gemini/antigravity-cli/antigravity-oauth-token`
are real. Chats, history, settings and trusted workspaces stay shared; the
credentials do not. The child is handed that directory as `$HOME`, so nothing
about your own shell changes.

Google does not rotate this refresh token and tolerates concurrent refreshes, so
none of codex's handle/lease machinery applies: the broker owns the account by
being what hands the token out. Accounts rank on Antigravity's own quota view,
tightest premium model first.

```sh
broker agy install          # broker-agy + shim, so plain `agy` goes through the broker
broker-agy auth <name>      # agy prints a Google URL; paste the code back
broker-agy list             # what is left on each account
AGY_ACCOUNT=<name> agy …    # pin one run through the environment
```

The repository does not ship a Google OAuth client credential. Set
`AGY_OAUTH_CLIENT_ID` and `AGY_OAUTH_CLIENT_SECRET` only for the
`broker seed agy` command; the broker stores them with that account's token.

agy also updates itself in place, over the very path the shim occupies — so the
wrapper sets `AGY_CLI_DISABLE_AUTO_UPDATE=1` on every run, and `broker-agy
upgrade` is how you move versions. For the same reason `broker agy install`
moves the real 178 MB binary into `lib/broker/real/` first; `broker agy
remove` puts it back.

`broker-cx upgrade` installs the CLI straight from the source repo, so a fix to the
wrapper is one push away — no publish step in between. Override the source per
machine with `src_repo`/`src_subdir` in the broker config, or install from a
local checkout with `broker-cx upgrade --from <dir>`. If the repo cannot be reached the
command stops and says so, rather than reaching for an older published copy.

## Rule

Once a provider is seeded, **don't run the bare CLI/app under that account** —
only the broker may refresh, or the rotation race returns. Use the wrapper.
