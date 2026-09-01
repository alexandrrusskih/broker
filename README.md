# @pbl/broker

Centralized OAuth **token broker** — the *sole refresh authority* for
`codex` / `claude` / `agy` across CI runners and dev machines. It owns the
rotating refresh token, refreshes it centrally under a lock, and hands out fresh
access tokens — so no two clients ever race and burn each other's refresh token
(`refresh_token_reused`).

The broker runs as Cloud Functions in a **dedicated Firebase project**. Refresh
tokens live in Firestore (Google-managed encryption at rest, no customer-managed
KMS). Deny-all client rules isolate the database; only the function's Admin SDK
accesses it. A Secret Manager value protects the atomic one-time bootstrap.

## Install

git is the only source. The npm registry copy trails this repo, so installing
from it would put an older CLI — and an older `broker-cx` — over a working setup.

```sh
bash install.sh          # clones the repo and installs the CLI globally
```

Or by hand, from a checkout:

```sh
bun install -g .  # or: npm install -g .
```

Updating later is `broker-cx upgrade` (CLI + wrapper + codex) or `broker upgrade`
(CLI only); both pull from the repo.

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
  BROKER_URL=$(jq -er '.url' ~/.config/hltm-broker/config.json)
  export CODEX_REFRESH_TOKEN_URL_OVERRIDE="$BROKER_URL/oauthRefresh?provider=codex&account=$ACCOUNT"
  export CODEX_REVOKE_TOKEN_URL_OVERRIDE="$BROKER_URL/oauthRevoke?provider=codex&account=$ACCOUNT"
fi
```

## Commands

| Command | What |
|---|---|
| `broker deploy --project <id> --dedicated-project [--alert-webhook <url>]` | Secure deploy + bootstrap + save config. |
| `broker seed <codex\|claude\|agy>` | Give the broker a freshly-logged-in refresh token. |
| `broker get <provider> [--format authjson\|raw]` | Fetch a fresh token (scripts/CI). |
| `broker <provider> install` | Install a `broker-cx`/`broker-cl`/`broker-agy` wrapper. |
| `broker accounts <provider>` | List the accounts seeded for a provider. |
| `broker forget <provider> --account <name> --yes` | Delete an account and its token from the broker. |
| `broker set-default <account>` | Set the default account for commands and wrappers. |
| `broker config [--url <url>] [--key <key>]` | Show/set local config. |

## Accounts

Tokens are isolated per account in the broker, and every command takes
`--account <name>`.

`broker-cl` (claude) resolves its account at run time: `$CLAUDE_ACCOUNT`, then
`$BROKER_ACCOUNT`, then an account pinned at install time, then the default from
`broker set-default`. It is a plain shell wrapper — two accounts share one auth
file — so give each its own profile through claude's own config-dir variable:

```sh
alias clcorp='CLAUDE_CONFIG_DIR=$HOME/.claude-corp CLAUDE_ACCOUNT=corp broker-cl'
```

`broker-cx` (codex) and `broker-agy` (agy) do none of that — they pick an account
per run and give each one its own profile. See below.

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
moves the real 178 MB binary into `lib/hltm-broker/real/` first; `broker agy
remove` puts it back.

`broker-cx upgrade` installs the CLI straight from the source repo, so a fix to the
wrapper is one push away — no publish step in between. Override the source per
machine with `src_repo`/`src_subdir` in the broker config, or install from a
local checkout with `broker-cx upgrade --from <dir>`. If the repo cannot be reached the
command stops and says so, rather than reaching for an older published copy.

## Rule

Once a provider is seeded, **don't run the bare CLI/app under that account** —
only the broker may refresh, or the rotation race returns. Use the wrapper.
