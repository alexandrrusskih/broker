// The one place that says what the CLI does. Kept out of cli.js so that the
// dispatch stays readable: thirty cases and an eighty-line string in one file
// hid both.
"use strict";

module.exports = `@pbl/broker — centralized OAuth token broker (sole refresh authority).

Providers: codex, claude, agy (OAuth) · glm (static z.ai key)
Each person uses their OWN account — pass --account <name> (or set it once via
'broker set-default <name>'); tokens are isolated per account in the broker.
The account is resolved per run: $CODEX_ACCOUNT / $CLAUDE_ACCOUNT / $AGY_ACCOUNT,
then $BROKER_ACCOUNT, then this machine's default — and with none of those set,
the wrapper picks the account with the most rate-limit headroom left.

Usage:
  broker setup [--url <url> | --client-config <file>] [--no-ask]
                         Configure a new client; existing saved connections are left untouched.
  broker setup --container [--client-config <file>] [--url <reachable-url>] [--image <image>] [--no-ask]
                         Configure Medulla Docker's Codex shim + client connection.
                         Checks real container access; offers Colima DNS repair with restart approval.
  broker server install [--url <url>] [--data-dir <dir>] [--port 8787] [--no-ask]
  broker server status|restart|stop
                         Install/manage a macOS SYSTEM LaunchDaemon; no GUI session required.
  broker init [--data-dir <dir>] [--url <private-https-url>]
                         Provision a self-hosted broker (JSON files, separate admin/client keys).
  broker serve [--data-dir <dir>] [--host 127.0.0.1] [--port 8787]
                         Run the self-hosted HTTP server. Put Tailscale Serve in front for HTTPS.
  broker deploy --project <firebase-id> --dedicated-project [--alert-webhook <url>]
                         Deploy to a DEDICATED Firebase project, install deny-all
                         client rules, mint the key securely, and save config.
  broker seed <provider> [--account <name>]
                         Hand the broker a freshly-logged-in refresh token (run after login).
  broker get <provider> [--format authjson|raw] [--account <name>]
                         Fetch a fresh token from the broker (for scripts/CI).
  broker wrap <provider> [--account <name>]
                         Install a wrapper (broker-cx/-cl/-agy) that pulls auth from the broker.
                         Without --account the wrapper follows the default account.
  broker status          What is installed, wired and seeded — start here.
  broker install <provider> [--default-account <name>] [--container]
  broker <provider> install|remove|status
                         Install the wrapper + shim (so plain 'codex' goes
                         through the broker), undo it, or show its state.
                         --default-account sets the account this machine sticks
                         to and skips the prompt — the form for image builds.
                         --container opts into creating a new Medulla host overlay.
  broker accounts <provider>
                         List the accounts seeded for a provider.
  broker forget <provider> --account <name> --yes
                         Delete an account from the broker (its token is gone).
  broker set-default <name>
                         Set the default account every command and wrapper uses.
  broker config [--url <url>] [--key <key>] [--account <name>]
                         Show or set local config (~/.config/broker/config.json).
  broker box build [--from <checkout>] [--claude <v>] [--codex <v>] [--bun <v>]
                   [--no-cache]
                         --from builds the Dockerfile in that checkout. Without it the
                         image comes from the broker that is running, which for an
                         installed copy is the version last installed.
  broker box list        Build the container image a --box runs in, or show what exists.
  broker box repair      Put a terminal back in order after a box was killed outright.
                         Then: claude --box <name> — the harness runs in a container
                         with only the directories that box names, at the same paths.
  broker upgrade [--all] [--server] [--from <checkout>]
                         Update CLI/wrappers; --all also updates harnesses.
                         --server additionally updates/restarts an installed server.
  broker version         Print the installed version.
  broker help

Onboarding a teammate (each brings their OWN codex):
  bash install.sh                                   # install the CLI
  broker config --url <broker-url> --key <key> --account alex
  codex login                                       # THEIR codex account
  broker seed codex                                 # their token → broker (account: alex)
  broker codex install                              # wrapper + shim: 'codex' uses their token, no race

The wrappers pick for you (broker-cx, broker-cl, broker-agy — and the shim means
plain 'codex'/'agy' reach them too):
  broker-cx                  runs on the account with the most headroom left
  broker-cx list             table of every account: plan, % used, when it resets
  broker-cx account account-a       run this one
  broker-cx auth account-b          log a new account in and seed it, in one command
  broker-cx delete-auth account-b   forget it again (asks for the name to confirm)
  broker-cx refresh          create/link a local profile for every seeded account
  broker-cx version          what is installed, and whether an update is due
  broker-cx upgrade          newest broker + wrappers from git, then update codex

Running a second account (e.g. 'account-b') from the same machine:
  broker-cx auth account-b                     # log it in and seed it, in one go
  broker-cx account account-b exec "..."       # one run on it, default untouched
  CODEX_ACCOUNT=account-b codex exec "..."     # same, through the environment
  broker set-default account-b                 # or make it the default

  Each account gets its own profile (~/.codex-<account>, ~/.agy-<account>), so
  parallel runs on different accounts never share a credentials file.
`;
