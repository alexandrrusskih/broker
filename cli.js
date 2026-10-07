#!/usr/bin/env node
"use strict";

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

const HELP = require("./lib/help");

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const { flags, positional } = parseFlags(rest);

  switch (cmd) {
    case "setup":
      await require("./lib/cmd/providers").setup(flags);
      break;
    case "server":
      await require("./lib/cmd/server").manage(flags, positional);
      break;
    case "init":
      await require("./lib/cmd/server").init(flags);
      break;
    case "serve":
      await require("./lib/cmd/server").serve(flags);
      break;
    case "deploy":
      await require("./lib/cmd/server").deploy(flags);
      break;
    case "seed":
      await require("./lib/cmd/tokens").seed(flags, positional);
      break;
    case "get":
      await require("./lib/cmd/tokens").get(flags, positional);
      break;
    case "wrap":
      require("./lib/cmd/providers").wrap(flags, positional);
      break;
    case "install":
    case "uninstall":
      await require("./lib/cmd/providers").installOrRemove(cmd, flags, positional);
      break;
    case "codex":
    case "claude":
    case "agy":
      await require("./lib/cmd/providers").one(cmd, flags, positional);
      break;
    case "status":
      await require("./lib/cmd/providers").status();
      break;
    case "accounts":
      await require("./lib/cmd/accounts").list(positional);
      break;
    case "forget":
      await require("./lib/cmd/accounts").forget(flags, positional);
      break;
    case "set-default":
      require("./lib/cmd/accounts").setDefault(flags, positional);
      break;
    case "config":
      require("./lib/cmd/settings").local(flags);
      break;
    case "config-set":
      await require("./lib/cmd/settings").remoteSet(flags, positional);
      break;
    case "config-get":
      await require("./lib/cmd/settings").remoteGet();
      break;
    case "upgrade":
      await require("./lib/cmd/upgrade")(flags);
      break;
    case "box":
      require("./lib/cmd/box").box(flags, positional);
      break;
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
