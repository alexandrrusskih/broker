# Self-hosted broker

One always-on server owns the refresh tokens. Host shims and Docker shims use
the same HTTP API, even when Docker runs on another machine. No SSH calls, shared
auth directories, Firebase account or SQLite are needed.

The Firebase deployment (`broker deploy`) remains supported with its existing
Firestore documents, broker key and rollout flag. Self-hosted (`broker serve`)
uses JSON files and **always issues Codex refresh handles**, never the real
refresh token. Other providers retain their existing behavior; the mid-session
refresh-handle mechanism described here is Codex-specific.

## 1. Provision the server

Requires Node >=20 and bun or npm. On macOS, run as the ordinary service user,
including over SSH; do not run the entire installer with sudo:

```sh
bash install.sh --server
```

It asks for the client-facing private HTTPS URL, provisions keys and registers
a **system LaunchDaemon**, not a GUI LaunchAgent. Sudo is requested only for
registration with launchd; the server runs as your ordinary user and does not
require a logged-in desktop session. Noninteractive setup:

```sh
bash install.sh --server --url https://broker-machine.example-tailnet.ts.net --no-ask
```

For an unpublished branch add `--from /absolute/path/to/checkout`. Without it,
the installer fetches the upstream default branch, even when run from another
branch. Optional `--data-dir` and `--port` select the initial data directory and
loopback port. Reinstall/upgrade preserves existing keys and accounts.

Installation prints config paths, not keys. It does not configure Tailscale,
copy Codex auth, install client shims, or stop an older refresh service. Do not
seed a grant until its old refresh authority has been retired (see below).

On other operating systems, automatic service setup is not provided. Run the
same server with your own supervisor, or use it in the foreground on macOS:

```sh
broker init --data-dir /Users/operator/.local/share/broker \
  --url https://broker-machine.example-tailnet.ts.net
broker serve --data-dir /Users/operator/.local/share/broker
```

Replace the sample URL with the server's actual Tailscale HTTPS name. `serve`
listens on `127.0.0.1:8787` by default. Initialization prints paths, not keys,
and does not change existing client config, Codex auth or any system service.
A repeat `init` refuses to replace existing keys.

The data directory contains:

- `settings.json`: server secrets, including the Codex handle-signing secret.
- `accounts/`: one private JSON file per account.
- `admin.json`: URL + admin key; for seeding, deletion and configuration.
- `client.json`: URL + client key; for fetching tokens and listing accounts.
- `logs/`: destination for the LaunchDaemon logs.

New directories are `0700`, secret files `0600`. These files are plaintext:
protect the server account, disk and backups. Do not distribute `admin.json`,
`settings.json` or `accounts/` to clients. The client key allows use of all seeded
accounts; it is not a per-account permission. Refresh handles are secrets too.

## 2. Private HTTPS through Tailscale

With the server joined to your tailnet and HTTPS enabled:

```sh
tailscale serve --bg http://127.0.0.1:8787
tailscale serve status
```

Use the HTTPS URL reported by Serve in the client configs. This is **Tailscale
Serve, not Funnel**: it is reachable within the permitted tailnet, not from the
public internet. Keep the broker on loopback; do not open its port on the router.
Restrict access to the intended machines with your tailnet access rules. The
broker key is still required in addition to network access.

The DNS name remains usable across IP/network changes while the node name and
tailnet domain remain unchanged. Tailscale must also run unattended on the host.
See [Serve](https://tailscale.com/docs/features/tailscale-serve) and
[IP/DNS addresses](https://tailscale.com/docs/concepts/ip-and-dns-addresses).

## 3. Move an account, then connect clients

Before moving a grant, stop its old refresh daemon and any unbrokered sessions
using that grant. **Two brokers holding the same rotating token are not safe.**
Use a fresh native Codex login, then seed through the admin config:

```sh
BROKER_CONFIG=/Users/operator/.local/share/broker/admin.json \
broker seed codex --account main
```

`admin.json` already contains the loopback URL. Remote clients use the
Tailscale HTTPS URL. A client key cannot seed or delete accounts. Admin wrapper
commands such as `broker-cx auth main` also need `BROKER_CONFIG=.../admin.json`.

Deliver **only** `client.json` to each client through your usual secure channel.
The installer can import it on a new client:

```sh
bash install.sh --client-config /path/to/client.json --no-ask
```

Or run `broker setup` to enter the URL and hidden client key interactively.
Setup saves `~/.config/broker/config.json` with mode `0600`, but leaves an
already configured client untouched. Use `BROKER_CONFIG` to select another
config file explicitly. Install the same wrapper on each host:

```sh
broker codex install --default-account main
codex exec "..."
```

Configuration works identically for Node CLI and Python wrappers:

- `BROKER_CONFIG`: alternate config file (default `~/.config/broker/config.json`).
- `BROKER_URL` and `BROKER_KEY`: fill missing config fields, usable without a file;
  saved URL/key take precedence, preserving existing installations.
- `CODEX_ACCOUNT` / `BROKER_ACCOUNT`: pin an account for a run, disabling automatic selection.

Keep admin credentials out of the environment of ordinary Codex sessions.
Runtime URL/key values are not persisted by config/account updates. `setup`
only saves a new connection when explicitly requested; env-only connections
are otherwise left in the environment.

At each wrapper start the broker returns a fresh-enough access token and an
opaque refresh handle. The shim routes Codex's own mid-session refresh back to
the broker. Refresh happens **on demand**, not through a daily auth-file copy.
The upstream Codex executable stays installed; the shim calls it, not itself.

A Medulla node retry starts a new `codex` process, so it goes through selection
and broker auth again when the shim is on PATH. An expired token is normally
refreshed for the same account. A broken account can be skipped in automatic
selection; a pinned account stays pinned. An unreachable broker may fall back
to cached auth, which is not a guarantee of recovery. No Medulla retry changes
are needed. Do not launch the workflow runner inside an already brokered Codex
process: nested-call guards and its shielded PATH intentionally bypass selection.

## 4. Docker, including a different host

Keep Node, Python 3 and native Codex installed in the image. Install the broker
from this checkout and run at image-build time, without credentials:

```sh
broker codex install --bin-dir /usr/local/bin --default-account main
```

Supply the client config at runtime, e.g. mount it read-only at
`/run/secrets/broker.json` and set `BROKER_CONFIG=/run/secrets/broker.json`.
Alternatively inject `BROKER_URL` + `BROKER_KEY` through your runtime secret
mechanism. Do not bake credentials into image layers or mount the server's
account files. The runtime user's home still needs to be writable for Codex
profiles, which contain access tokens and handles.

For Medulla, configure the overlay once on the **Docker host**. This is the same
command for Docker Desktop and Colima:

```sh
broker setup --container
# Or use a securely delivered client config without configuring the host CLI:
broker setup --container --client-config /path/to/client.json
```

This populates:

- `~/.medulla/container/bin/broker-cx`: self-contained Python zipapp.
- `~/.medulla/container/home/.local/bin/codex`: shim ahead of native Codex on PATH.
- `~/.medulla/container/home/.config/broker/config.json`: client connection,
  written atomically with mode `0600`.

The command uses an explicit `--client-config` first, then an existing overlay,
then the host client. It copies only connection/account fields, rejects a config
marked `admin`, and never copies native Codex auth or reads server settings.
On a managed server whose host client uses loopback, it discovers that same
server's external client URL from its service metadata and `client.json`.
Otherwise pass `--url <reachable-url>`; `localhost`/`127.0.0.1` would point at the
container, not its host, and are rejected. The host client/shim are left alone.

This default config location tells Medulla's initialization not to copy native
host auth. Apply the overlay on the actual Docker host. Once the overlay exists,
ordinary wrapper upgrades continue updating it without requiring `--container`.
The older `broker codex install --container` still installs/updates wrappers
without provisioning a client config; its behavior is unchanged.
The real `/usr/local/bin/codex` and its npm target must not be overwritten by a
bind mount. Recreate an already running container to pick up new mounts.

**Setup verifies real container access.** A temporary read-only container mounts
only the client config and requests `/listAccounts?provider=codex` on the default
Docker network: no `--dns`, `--add-host`, token refresh or model call. The key is
never an argument or environment variable, TLS verification stays enabled and
redirects are refused. A 401 is a failed key check, not success. Zero seeded Codex
accounts also leaves setup incomplete. Failure exits nonzero; the prepared
overlay stays on disk so setup can be rerun after fixing the connection.

The check uses the official `python:3.12-slim` image, pulled if missing. To use a
trusted local workflow image with Python 3, pass `--image <image>`. This checks
its broker connection, not native Codex installation or every workflow/image.
An actual Medulla smoke run is still the rollout check for shim/mount behavior.
Remote Docker endpoints are rejected: run setup on the Docker host, where the
overlay files live. Docker must already be running.

Older `broker upgrade` versions may only link the source checkout. On first
container setup, missing runtime dependencies are installed automatically using
npm or Bun in that checkout, with package lifecycle scripts disabled. Existing
dependencies are reused; no second upgrade or manual npm command is needed.

If DNS fails on a local Colima profile, setup matches the exact broker hostname
against `tailscale status --json` and offers its Tailscale IPv4 as one `dnsHosts`
entry. Other config and mappings are preserved; custom DNS resolver lists are
not overwritten (they disable `dnsHosts`). Setup shows running containers and
asks before changing config or restarting Colima. A backup is saved beside
`colima.yaml`. After the restart, the ordinary container check must pass before
setup reports ready. Repeat runs with working access do not restart anything.

`--no-ask` or a declined prompt never changes DNS or restarts a service. Docker
Desktop/other runtimes, unknown hosts and other network failures get a specific
error; setup does not reconfigure them or change Tailscale itself. Broker server
processes and host Codex sessions are untouched by a Colima restart. See
[Tailscale in Docker](https://tailscale.com/docs/features/containers/docker).

## 5. Manage and upgrade the system daemon

The managed macOS installation uses:

- `/Library/LaunchDaemons/com.broker.server.plist`: system registration.
- `~/.local/share/broker-service/runtime`: private snapshot of server code.
- `~/.local/share/broker-service/service.json`: service paths/settings, no tokens.
- `~/.local/share/broker`: data and keys, unless `--data-dir` was specified.

The runtime is separate from the CLI's git cache/global package, so an ordinary
client upgrade does not change files underneath the running server. Management:

```sh
broker server status
broker server restart
broker server stop    # stays registered for next boot; restart starts it now
broker upgrade --server
```

`broker upgrade` updates the CLI and installed wrappers only. `--all` also
updates native harnesses, retaining its existing meaning; it does not imply
`--server`. `--server` additionally stages server code, checks that it loads,
stops the old daemon and waits for its process to finish before swapping code.
It restarts and checks the API using the client key, without requesting OAuth
tokens. If startup fails, it restores the previous runtime and restarts it.
**Token files are never rolled back.** Data, admin/client keys and the handle
secret are preserved. The data directory cannot be changed through an upgrade.

While testing an unpublished branch:

```sh
broker upgrade --server --from /absolute/path/to/checkout
```

The installer can also be rerun with `--server --from ...`. An unmanaged service
with the same label is not overwritten. Launchd registration needs sudo; a truly
unattended update therefore needs appropriate OS permissions already configured.
Tailscale exposure remains a separate step. A successful loopback health check
does not mean remote clients have working tailnet routing.

Run **one process per data directory**. Do not run multiple replicas, ports or
worker processes over these files, and do not put the directory on a network
share. For multiple server instances use Firestore. Tailscale Serve handles
the private TLS endpoint separately; no assessment workflow needs to know how
the broker is supervised.

## Refresh safety and recovery

For each account, the broker atomically records `rotation_pending` + lease owner
before calling the OAuth provider. Other callers wait. A successful rotation
is written through a private temp file, fsync and rename before being returned.
Short per-account queues serialize file writes; network calls do not block
other accounts. Failure cleanup and successful writes both check ownership,
so a concurrent re-seed/deletion cannot be overwritten by an older refresh.

If a crash or timeout leaves the outcome unknown, restarting the daemon does
not retry the possibly consumed refresh token. That account requires a fresh
login and admin re-seed; do not clear the journal by hand. SIGTERM/SIGINT drain
in-flight requests, and the LaunchDaemon gives shutdown 120 seconds.

Do not restore an old token backup and resume refreshing blindly: it may contain
an already-consumed refresh token. Re-login/re-seed affected accounts instead.

## Tests

`node --test test/*.test.js` uses temporary files, fake OAuth tokens and a
loopback-only HTTP server. It does not contact OpenAI or a Firebase deployment.
Service and installer tests mock launchctl/sudo/package installation: they never
install a real service or change the machine's broker or native Codex auth.
Firestore is covered by adapter contract tests, not a live cloud integration.
Native Codex and real Docker/Tailscale connectivity need a separate rollout
smoke test after provisioning; no services are installed by the test suite.
