// Installing a provider's wrapper and shim, removing them, and saying how it
// all stands.
"use strict";

const config = require("../config");

// `broker install codex` reads better in a Dockerfile than `broker codex
// install`. Both reach the same place.
async function installOrRemove(cmd, flags, positional) {
  // `broker install codex` reads better in a Dockerfile than
  // `broker codex install`; both reach the same place.
  const providerCmd = require("../provider-cmd");
  const { WRAP } = require("../wrap");
  const provider = positional[0];
  if (!WRAP[provider]) {
    throw new Error(
      `usage: broker ${cmd} <${Object.keys(WRAP).join("|")}> [--default-account <name>]`
    );
  }
  if (cmd === "install") await providerCmd.install(provider, flags);
  else providerCmd.remove(provider);
}

async function one(cmd, flags, positional) {
  const providerCmd = require("../provider-cmd");
  const action = positional[0] || "status";
  if (!["install", "remove", "status"].includes(action)) {
    throw new Error(`usage: broker ${cmd} <install|remove|status>`);
  }
  if (action === "install") await providerCmd.install(cmd, flags);
  else if (action === "remove") providerCmd.remove(cmd);
  else await providerCmd.status(cmd);
}

async function status() {
  const providerCmd = require("../provider-cmd");
  const { WRAP } = require("../wrap");
  const cfg = config.read();
  const color = require("../color");
  console.log(`${color.bold("broker")}    ${require("../../package.json").version}`);
  console.log(`config    ${color.dim(config.FILE)}`);
  console.log(`url       ${cfg.url ? cfg.url : color.red("not set — run 'broker config --url <url>'")}`);
  console.log(
    `key       ${cfg.key ? color.green("set") : color.red("MISSING — run 'broker config --key <broker_key>'")}`
  );
  console.log("");
  for (const provider of Object.keys(WRAP)) {
    await providerCmd.status(provider);
  }
}

async function setup(flags) {
  if (flags.container === true) {
    const out = await require("../container-setup").setupContainer({
      url: flags.url, clientConfig: flags["client-config"], image: flags.image, noAsk: flags["no-ask"] === true
    });
    console.log(`Medulla container Codex overlay ready: ${out.wrapper}`);
    console.log(`Client config: ${out.file}\nBroker URL: ${out.url}`);
    console.log(`Container connection verified: ${out.network.accounts} Codex account(s); authenticated broker response on the default network.`);
    console.log("New Medulla containers use this setup. Recreate already-running containers to pick it up.");
    console.log("Host Codex and broker server/accounts were not changed.");
    return;
  }
  const out = await require("../setup").setup({
    url: flags.url, clientConfig: flags["client-config"], noAsk: flags["no-ask"] === true
  });
  console.log(out.changed ? `Client connection saved: ${out.file}`
    : out.configured ? "Existing client connection preserved."
    : "Client setup skipped; use broker setup or provide a runtime config when ready.");
}

function wrap(flags, positional) {
  const { install } = require("../wrap");
  const provider = positional[0];
  if (!provider) throw new Error("usage: broker wrap <codex|claude|agy> [--account <name>]");
  // No --account: leave the wrapper unpinned so it follows the default
  // account at run time (set-default then applies without re-wrapping).
  const pinned = flags.account ? String(flags.account) : null;
  const { WRAP: wrapTable } = require("../wrap");
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
  // Only a harness whose credentials are a FILE keeps a profile per account
  // on disk. claude carries its token in the environment and has none, so
  // this line used to tell it that its accounts live in ~/.agy-<account>.
  const profileBase = { codex: "~/.codex-<account>", agy: "~/.agy-<account>" }[provider];
  if (picksPerRun && profileBase) {
    console.log(`  profiles: every account gets ${profileBase}`);
  }
  if (!r.inPath) console.log(`  note: ${require("path").dirname(r.path)} is not in PATH — add it`);
  for (const old of r.dropped || []) console.log(`  removed the old name ${old}`);
  // An updater may have taken the native name back since last time.
  if (require("../shim").ensure(provider)) {
    console.log(`  restored the '${require("../wrap").WRAP[provider].bin}' shim`);
  }
  console.log(`  use '${r.cmd}' instead of the bare CLI from now on`);
}
module.exports = { installOrRemove, one, status, setup, wrap };
