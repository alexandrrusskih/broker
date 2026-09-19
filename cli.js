#!/usr/bin/env node
"use strict";

const config = require("./lib/config");

function execSyncQuiet(cmd) {
  return require("child_process").execSync(cmd, { stdio: "ignore" });
}

function parseFlags(argv) {
  const flags = {};
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i++;
      }
    } else {
      positional.push(a);
    }
  }
  return { flags, positional };
}

const HELP = `@pbl/broker — centralized OAuth token broker (sole refresh authority).

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
  broker box build [--claude <v>] [--codex <v>] [--no-cache]
  broker box list        Build the container image a --box runs in, or show what exists.
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

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const { flags, positional } = parseFlags(rest);

  switch (cmd) {
    case "setup": {
      if (flags.container === true) {
        const out = await require("./lib/container-setup").setupContainer({
          url: flags.url, clientConfig: flags["client-config"], image: flags.image, noAsk: flags["no-ask"] === true
        });
        console.log(`Medulla container Codex overlay ready: ${out.wrapper}`);
        console.log(`Client config: ${out.file}\nBroker URL: ${out.url}`);
        console.log(`Container connection verified: ${out.network.accounts} Codex account(s); authenticated broker response on the default network.`);
        console.log("New Medulla containers use this setup. Recreate already-running containers to pick it up.");
        console.log("Host Codex and broker server/accounts were not changed.");
        break;
      }
      const out = await require("./lib/setup").setup({
        url: flags.url, clientConfig: flags["client-config"], noAsk: flags["no-ask"] === true
      });
      console.log(out.changed ? `Client connection saved: ${out.file}`
        : out.configured ? "Existing client connection preserved."
        : "Client setup skipped; use broker setup or provide a runtime config when ready.");
      break;
    }
    case "server": {
      const manager = require("./lib/service").createServiceManager();
      const action = positional[0] || "status";
      if (action === "install") {
        const out = await manager.install({
          url: flags.url, dataDir: flags["data-dir"], port: flags.port,
          from: flags.from, noAsk: flags["no-ask"] === true
        });
        console.log(`System broker ${out.updated ? "updated" : "installed"} and ready on 127.0.0.1:${out.port}`);
        console.log(`Runtime: ${out.runtime}\nData: ${out.dataDir}`);
        console.log(`Client config: ${out.clientFile}\nAdmin config: ${out.adminFile} (keep on the server)`);
        console.log(`Client URL: ${out.url}`);
        console.log(`Private HTTPS is separate: tailscale serve --bg http://127.0.0.1:${out.port}`);
        console.log("No client shim, Tailscale setting, old refresh daemon or existing Codex auth was changed.");
      } else if (action === "status") {
        const out = await manager.status();
        console.log(`system/${require("./lib/service").LABEL}: ${out.healthy ? "ready" : out.pid ? "running, API not ready" : "stopped"}`);
        console.log(`User: ${out.user}\nRuntime: ${out.runtime}\nData: ${out.dataDir}\nClient URL: ${out.url}`);
        if (!out.healthy) process.exitCode = 1;
      } else if (action === "restart") {
        await manager.restart();
        console.log("System broker restarted and ready.");
      } else if (action === "stop") {
        await manager.stop();
        console.log("System broker stopped. Use broker server restart to start it again; its boot-time registration is retained.");
      } else throw new Error("usage: broker server install|status|restart|stop");
      break;
    }
    case "init": {
      const out = await require("./lib/server").init({ dataDir: flags["data-dir"], url: flags.url });
      console.log(`Self-hosted broker initialized: ${out.dataDir}`);
      console.log(`Client config: ${out.clientFile}`);
      console.log(`Admin config:  ${out.adminFile} (keep on the server)`);
      console.log("Use BROKER_CONFIG=<file> with CLI/wrappers. No services or existing auth were changed.");
      break;
    }
    case "serve": {
      const port = flags.port === undefined ? 8787 : Number(flags.port);
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("invalid port");
      const server = await require("./lib/server").serve({ dataDir: flags["data-dir"], host: flags.host, port });
      console.log(`Broker listening on ${server.address().address}:${server.address().port}`);
      // Drain in-flight rotations before launchd restarts the process.
      const stop = () => server.close((err) => { if (err) { console.error(err.message); process.exitCode = 1; } });
      process.once("SIGTERM", stop);
      process.once("SIGINT", stop);
      break;
    }
    case "deploy": {
      const { deploy } = require("./lib/deploy");
      const out = await deploy({
        project: flags.project,
        alertWebhook: flags["alert-webhook"],
        dedicatedProject: flags["dedicated-project"] === true
      });
      console.log(`\n✓ broker deployed: ${out.url}`);
      console.log(`  config saved to ${config.FILE}`);
      console.log(`  next: log in to a provider, then 'broker seed <provider>'`);
      break;
    }
    case "seed": {
      const { seed } = require("./lib/seed");
      const provider = positional[0];
      if (!provider) throw new Error("usage: broker seed <codex|claude|agy|glm> [--account <name>]");
      const account = flags.account || config.read().account;
      if (!account) throw new Error(`which account? pass --account <name> (or set one with 'broker set-default')`);
      await seed(provider, account);
      console.log(`✓ seeded ${provider} (account: ${account}) — broker refresh confirmed (200)`);
      break;
    }
    case "get": {
      const { get } = require("./lib/get");
      const provider = positional[0];
      if (!provider) throw new Error("usage: broker get <provider> [--format authjson|raw] [--account <name>]");
      const account = flags.account || config.read().account;
      if (!account) throw new Error(`which account? pass --account <name> (or set one with 'broker set-default')`);
      const body = await get(provider, flags.format || "raw", account);
      process.stdout.write(body.endsWith("\n") ? body : body + "\n");
      break;
    }
    case "wrap": {
      const { install } = require("./lib/wrap");
      const provider = positional[0];
      if (!provider) throw new Error("usage: broker wrap <codex|claude|agy> [--account <name>]");
      // No --account: leave the wrapper unpinned so it follows the default
      // account at run time (set-default then applies without re-wrapping).
      const pinned = flags.account ? String(flags.account) : null;
      const { WRAP: wrapTable } = require("./lib/wrap");
      // Wrappers built from the python engine (codex, agy) resolve the account per
      // run — pinning one at wrap time would be silently ignored, so refuse it
      // rather than pretend. This used to test for `codex` by name, which left agy
      // accepting a pin that did nothing.
      const picksPerRun = Boolean(wrapTable[provider].template);
      if (pinned && picksPerRun) {
        throw new Error(
          `${wrapTable[provider].cmd} takes no pinned account — name one per run ` +
            `('${wrapTable[provider].cmd} account <name>') or let it pick`
        );
      }
      const r = install(provider, pinned, undefined, { container: flags.container === true });
      const shown = r.pinned || (picksPerRun ? "named per run, or picked" : `${config.read().account || "default"}, follows the default`);
      console.log(`✓ installed wrapper '${r.cmd}' → ${r.path} (account: ${shown})`);
      if (picksPerRun) {
        const base = provider === "codex" ? "~/.codex-<account>" : "~/.agy-<account>";
        console.log(`  profiles: every account gets ${base}`);
      }
      if (!r.inPath) console.log(`  note: ${require("path").dirname(r.path)} is not in PATH — add it`);
      for (const old of r.dropped || []) console.log(`  removed the old name ${old}`);
      // An updater may have taken the native name back since last time.
      if (require("./lib/shim").ensure(provider)) {
        console.log(`  restored the '${require("./lib/wrap").WRAP[provider].bin}' shim`);
      }
      console.log(`  use '${r.cmd}' instead of the bare CLI from now on`);
      break;
    }
    case "install":
    case "uninstall": {
      // `broker install codex` reads better in a Dockerfile than
      // `broker codex install`; both reach the same place.
      const providerCmd = require("./lib/provider-cmd");
      const { WRAP } = require("./lib/wrap");
      const provider = positional[0];
      if (!WRAP[provider]) {
        throw new Error(
          `usage: broker ${cmd} <${Object.keys(WRAP).join("|")}> [--default-account <name>]`
        );
      }
      if (cmd === "install") await providerCmd.install(provider, flags);
      else providerCmd.remove(provider);
      break;
    }

    case "codex":
    case "claude":
    case "agy": {
      const providerCmd = require("./lib/provider-cmd");
      const action = positional[0] || "status";
      if (!["install", "remove", "status"].includes(action)) {
        throw new Error(`usage: broker ${cmd} <install|remove|status>`);
      }
      if (action === "install") await providerCmd.install(cmd, flags);
      else if (action === "remove") providerCmd.remove(cmd);
      else await providerCmd.status(cmd);
      break;
    }

    case "status": {
      const providerCmd = require("./lib/provider-cmd");
      const { WRAP } = require("./lib/wrap");
      const cfg = config.read();
      const color = require("./lib/color");
      console.log(`${color.bold("broker")}    ${require("./package.json").version}`);
      console.log(`config    ${color.dim(config.FILE)}`);
      console.log(`url       ${cfg.url ? cfg.url : color.red("not set — run 'broker config --url <url>'")}`);
      console.log(
        `key       ${cfg.key ? color.green("set") : color.red("MISSING — run 'broker config --key <broker_key>'")}`
      );
      console.log("");
      for (const provider of Object.keys(WRAP)) {
        await providerCmd.status(provider);
      }
      break;
    }

    case "accounts": {
      const cfg = config.require();
      const provider = positional[0];
      if (!provider) throw new Error("usage: broker accounts <codex|claude|agy|glm>");
      const r = await fetch(`${cfg.url}/listAccounts?provider=${encodeURIComponent(provider)}`, {
        headers: { "x-broker-key": cfg.key }
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(`listAccounts ${r.status}: ${body.error || "failed"}`);
      const current = cfg.account || "default";
      const accounts = body.accounts || [];
      if (!accounts.length) {
        console.log(`no ${provider} accounts seeded — run 'broker seed ${provider} --account <name>'`);
        break;
      }
      for (const a of accounts) console.log(a === current ? `* ${a}` : `  ${a}`);
      break;
    }
    case "forget": {
      const cfg = config.require();
      const provider = positional[0];
      const account = flags.account || positional[1];
      if (!provider || !account || account === true) {
        throw new Error("usage: broker forget <provider> --account <name>");
      }
      if (!flags.yes) throw new Error(`refusing without --yes: this drops ${provider}/${account}'s refresh token for good`);
      const r = await fetch(
        `${cfg.url}/deleteAccount?provider=${encodeURIComponent(provider)}&account=${encodeURIComponent(account)}`,
        { method: "POST", headers: { "x-broker-key": cfg.key } }
      );
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(`deleteAccount ${r.status}: ${body.error || "failed"}`);
      console.log(body.existed ? `✓ ${provider}/${account} deleted` : `${provider}/${account} was not there`);
      // Deleting the account in the broker used to leave its token on this
      // machine — and for agy that is the real refresh token, so "deleted" meant
      // deleted in one place only. History stays; credentials do not.
      for (const gone of dropCredentials(provider, account)) {
        console.log(`  removed its credentials: ${gone}`);
      }
      break;
    }
    case "set-default": {
      const account = positional[0] || flags.account;
      if (!account || account === true) throw new Error("usage: broker set-default <account>");
      const before = config.read().account || "default";
      config.write({ account: String(account) });
      console.log(`✓ default account: ${before} -> ${account} (${config.FILE})`);
      console.log(`  broker-cx and broker-agy pick per run; broker-cl follows this default`);
      break;
    }
    case "config": {
      if (flags.url || flags.key || flags.account) {
        const patch = {};
        if (flags.url) patch.url = String(flags.url).replace(/\/$/, "");
        if (flags.key) patch.key = flags.key;
        if (flags.account) patch.account = flags.account;
        config.write(patch);
        console.log(`✓ config updated (${config.FILE})`);
      }
      const c = config.read();
      console.log(JSON.stringify({ url: c.url || null, key: c.key ? "<set>" : null, account: c.account || "default", project: c.project || null }, null, 2));
      break;
    }
    case "config-set": {
      // Set a whitelisted broker_config field (the codex rollout flag / handle
      // secret). This is the supported way to flip the rollout — see §6.8.
      const cfg = config.require();
      const key = positional[0];
      let value = positional[1];
      if (!key) throw new Error("usage: broker config-set codex_handle_rollout <true|false>\n       broker config-set codex_handle_secret <hex|generate>");
      if (key === "codex_handle_secret" && (value === "generate" || flags.generate)) {
        value = require("crypto").randomBytes(32).toString("hex");
        console.log("  (generated a random 32-byte secret; provision it BEFORE flipping the flag — see §6.8)");
      }
      const r = await fetch(`${cfg.url}/configSet`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-broker-key": cfg.key },
        body: JSON.stringify({ key, value })
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(`configSet ${r.status}: ${body.error || "failed"}${body.settable ? " (settable: " + body.settable.join(", ") + ")" : ""}`);
      console.log(`✓ ${key} = ${body.value}`);
      if (key === "codex_handle_rollout" && body.value === true) {
        console.log("  ⚠ handles are now issued — make sure every cx/CI is updated and NO long codex session is live (§3 collision).");
      }
      break;
    }
    case "config-get": {
      const cfg = config.require();
      const r = await fetch(`${cfg.url}/configGet`, { headers: { "x-broker-key": cfg.key } });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(`configGet ${r.status}: ${body.error || "failed"}`);
      console.log(`codex_handle_rollout:    ${body.codex_handle_rollout}`);
      console.log(`codex_handle_secret_set: ${body.codex_handle_secret_set}`);
      break;
    }
    case "upgrade": {
      const { execFileSync } = require("child_process");
      const os = require("os");
      const pathMod = require("path");
      const fs = require("fs");
      // git is the only source: the npm registry copy trails this repo, and
      // installing from it downgrades a working setup.
      const cfg = config.read();
      require("./lib/legacy").dropCaches(); // DROP AFTER 2026-12
      if (flags.server) await require("./lib/service").createServiceManager().requireInstalled();
      console.log(`current: ${require("./package.json").version}`);
      const pkg = require("./lib/source").sourcePackage(cfg, flags.from);
      if (flags.server && !fs.existsSync(pathMod.join(pkg, "lib", "service.js"))) {
        throw new Error("selected source does not support managed self-hosted servers; server and CLI were not changed");
      }
      let installed = false;
      for (const tool of ["bun", "npm"]) {
        try {
          execSyncQuiet(`${tool} --version`);
        } catch (_e) {
          continue;
        }
        // Remove first: installing the same path again appends a duplicate to
        // bun's global manifest instead of replacing the entry.
        try {
          execFileSync(tool, ["remove", "-g", "@pbl/broker"], { stdio: "ignore" });
        } catch (_e) {
          // not installed yet
        }
        execFileSync(tool, ["install", "-g", pkg], { stdio: "inherit" });
        installed = true;
        break;
      }
      if (!installed) throw new Error("need bun or npm to install");
      console.log(`installed from ${pkg}`);

      // The wrappers on disk carry a COPY of the engine, so a new CLI alone
      // changes nothing about what runs when you type `codex`. This used to be a
      // hint telling people to go run another command; the hint named `cx`, which
      // no longer exists, and `broker wrap`, which installs the wrapper without
      // the shim — leaving the bare harness outside the broker. So do it here
      // instead, for whatever is already installed, through the CLI we just put
      // down (this process is still the old code).
      const { WRAP: wrapTable } = require("./lib/wrap");
      const shimCfg = config.read();
      const installedProviders = Object.keys(wrapTable).filter(
        (name) =>
          (shimCfg.shims || []).includes(name) ||
          fs.existsSync(pathMod.join(shimCfg.bin_dir || pathMod.join(os.homedir(), ".local", "bin"), wrapTable[name].cmd))
      );
      for (const name of installedProviders) {
        try {
          execFileSync("broker", [name, "install", "--no-ask"], { stdio: "inherit" });
        } catch (_e) {
          console.log(`  could not refresh the ${name} wrapper — run 'broker ${name} install'`);
        }
      }
      if (!installedProviders.length) {
        console.log(`  next: 'broker codex install' (wrapper + shim, so plain 'codex' goes through the broker)`);
      }

      // --all also updates the harnesses themselves. Without it this command
      // touches only the broker, and updating everything meant remembering one
      // `broker-XX upgrade` per provider — each of which re-did the broker part.
      if (flags.all || flags.harnesses) {
        for (const name of installedProviders) {
          const w = wrapTable[name];
          console.log(`\n→ updating ${w.bin}...`);
          try {
            // Through the wrapper, so `update` reaches the real binary; then the
            // shim goes back, because the updater writes its own launcher over it.
            execFileSync(w.cmd, ["update"], { stdio: "inherit" });
          } catch (_e) {
            console.log(`  ${w.bin} update failed — run '${w.cmd} upgrade' to see why`);
          }
          try {
            execFileSync("broker", [name, "install", "--no-ask"], { stdio: "inherit" });
          } catch (_e) {
            console.log(`  could not restore the ${name} shim — run 'broker ${name} install'`);
          }
        }
      }
      if (flags.server) {
        // Use the new source, not modules already loaded by this old CLI.
        execFileSync(process.execPath, [pathMod.join(pkg, "cli.js"), "server", "install", "--no-ask", "--from", pkg], { stdio: "inherit" });
      }
      break;
    }
    case "box": {
      const boxes = require("./lib/box");
      const action = positional[0] || "list";
      if (action === "build") {
        const out = boxes.build({
          image: flags.image, claude: flags.claude, codex: flags.codex,
          noCache: flags["no-cache"] === true, runtime: flags.runtime
        });
        console.log(`Image ready: ${out.image}`);
        if (boxes.seedProfiles()) console.log(`Wrote an example box file: ${boxes.PROFILES}`);
        console.log(`Define boxes in ${boxes.PROFILES}, then: claude --box <name>`);
      } else if (action === "list" || action === "ls") {
        const out = boxes.list();
        console.log(`file    ${out.file}`);
        console.log(`image   ${out.image ? `${boxes.IMAGE} (built)` : `${boxes.IMAGE} — not built, run 'broker box build'`}`);
        if (!out.runtime) console.log("docker  not reachable — start Docker Desktop");
        if (!out.boxes) {
          console.log("\nNo boxes defined yet. 'broker box build' writes an example file.");
          break;
        }
        for (const [name, box] of Object.entries(out.boxes)) {
          const rw = (box.rw || []).join(", ") || "—";
          const ro = (box.ro || []).length ? `  ro: ${box.ro.join(", ")}` : "";
          console.log(`\n${name}\n  rw: ${rw}${ro ? "\n" + ro : ""}`);
        }
      } else {
        throw new Error(`unknown box command '${action}' — use 'build' or 'list'`);
      }
      break;
    }
    case "version":
    case "--version":
    case "-v":
      console.log(require("./package.json").version);
      break;
    case "help":
    case undefined:
      process.stdout.write(HELP);
      break;
    default:
      process.stderr.write(`unknown command: ${cmd}\n\n${HELP}`);
      process.exit(1);
  }
}

main().catch((e) => {
  process.stderr.write(`error: ${e.message}\n`);
  process.exit(1);
});

// Where an account's credentials sit on this machine, for the two shapes that
// exist: a file inside a per-account profile, and the wrapper's own cache.
function dropCredentials(provider, account) {
  const fsMod = require("fs");
  const pathMod = require("path");
  const osMod = require("os");
  const { WRAP } = require("./lib/wrap");
  const w = WRAP[provider];
  const paths = [
    pathMod.join(config.CACHE_DIR, `${provider}-${account}.json`)
  ];
  if (w && w.profileBase && w.authRel) {
    paths.push(pathMod.join(osMod.homedir(), `${w.profileBase}-${account}`, w.authRel));
  }
  const removed = [];
  for (const target of paths) {
    try {
      if (!fsMod.existsSync(target)) continue;
      fsMod.rmSync(target);
      removed.push(target);
    } catch (_e) {
      // a credential we cannot remove is worth saying nothing about here; the
      // account is already gone from the broker either way
    }
  }
  return removed;
}
