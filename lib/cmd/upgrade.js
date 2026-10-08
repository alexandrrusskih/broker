// Updating the broker, the wrappers, and — with --all — the harnesses behind
// them. git is the only source: the npm registry copy trails this repo, and
// installing from it downgrades a working setup.
"use strict";

const config = require("../config");

function execSyncQuiet(cmd) {
  return require("child_process").execSync(cmd, { stdio: "ignore" });
}

module.exports = async function upgrade(flags) {
  const { execFileSync } = require("child_process");
  const os = require("os");
  const pathMod = require("path");
  const fs = require("fs");
  const conciseError = (error) => {
    const lines = String(error.stderr || error.stdout || error.message || error)
      .trim().split(/\r?\n/).filter(Boolean);
    return lines.slice(-2).join(" | ") || `exit ${error.status ?? "unknown"}`;
  };
  let failures = 0;
  // git is the only source: the npm registry copy trails this repo, and
  // installing from it downgrades a working setup.
  const cfg = config.read();
  require("../legacy").dropCaches(); // DROP AFTER 2026-12
  if (flags.server) await require("../service").createServiceManager().requireInstalled();
  console.log(`current: ${require("../../package.json").version}`);
  const pkg = require("../source").sourcePackage(cfg, flags.from);
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
    try {
      execFileSync(tool, ["install", "-g", pkg], { stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      throw new Error(`broker install via ${tool} failed: ${conciseError(error)}`);
    }
    installed = true;
    console.log(`broker: installed via ${tool} from ${pkg}`);
    break;
  }
  if (!installed) throw new Error("need bun or npm to install");

  // The wrappers on disk carry a COPY of the engine, so a new CLI alone
  // changes nothing about what runs when you type `codex`. This used to be a
  // hint telling people to go run another command; the hint named `cx`, which
  // no longer exists, and `broker wrap`, which installs the wrapper without
  // the shim — leaving the bare harness outside the broker. So do it here
  // instead, for whatever is already installed, through the source CLI.
  // PATH may still resolve an older broker from another package manager.
  const { WRAP: wrapTable } = require("../wrap");
  const shimCfg = config.read();
  const installedProviders = Object.keys(wrapTable).filter(
    (name) =>
      (shimCfg.shims || []).includes(name) ||
      fs.existsSync(pathMod.join(shimCfg.bin_dir || pathMod.join(os.homedir(), ".local", "bin"), wrapTable[name].cmd))
  );
  for (const name of installedProviders) {
    try {
      execFileSync(process.execPath,
        [pathMod.join(pkg, "cli.js"), "install", name, "--no-ask", "--quiet"],
        { stdio: ["ignore", "pipe", "pipe"] });
      if (!flags.all && !flags.harnesses) console.log(`${name} wrapper: ready`);
    } catch (error) {
      failures++;
      console.error(`${name} wrapper: FAILED — ${conciseError(error)}`);
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
      const version = () => {
        try {
          return String(execFileSync(w.cmd, ["--version"], { stdio: ["ignore", "pipe", "pipe"] }) || "")
            .trim().split(/\r?\n/)[0] || null;
        } catch (_e) {
          return null;
        }
      };
      const before = version();
      let updateFailed = false;
      // Every harness gets its updater run, standalone codex included. It used
      // to be skipped here, on the reasoning that a standalone install keeps
      // itself current — and it does not: `check_for_update_on_startup` is off
      // in ~/.codex/config.toml on purpose, because codex's own updater writes
      // its launcher over ~/.local/bin/codex and takes codex off the broker.
      // So nobody updated it and `--all` still printed a version, which read as
      // success; codex sat three minors behind for a week. The reason for the
      // skip is already handled two statements down, where the shim goes back.
      try {
        // Through the wrapper, so the update reaches the real binary; then the
        // shim goes back, because the updater writes its own launcher over it.
        execFileSync(w.cmd, [w.updateCommand || "update"], { stdio: ["ignore", "pipe", "pipe"] });
      } catch (error) {
        updateFailed = true;
        failures++;
        console.error(`${w.bin}: FAILED — ${conciseError(error)}`);
      }
      // An updater installs beside the old version and leaves the launcher
      // alone, so without this the update lands on disk and never runs.
      try {
        require("../shim").adoptNewestVersion(name);
      } catch (_e) {
        // nothing to adopt, or not a harness that versions itself this way
      }
      try {
        execFileSync("broker", ["install", name, "--no-ask", "--quiet"], { stdio: ["ignore", "pipe", "pipe"] });
      } catch (error) {
        failures++;
        console.error(`${w.bin} shim: FAILED — ${conciseError(error)}`);
      }
      if (!updateFailed) {
        const after = version();
        console.log(`${w.bin}: ${before && after ? `${before} → ${after}` : after || "updated"}`);
      }
    }
  }
  // A box is meant to reproduce THIS machine, so the image pins follow what
  // the machine now has — but only after the machine has actually been
  // brought up to date. A plain `upgrade` updates the broker and nothing
  // else, and pinning there wrote whatever happened to be installed:
  // someone running it on an older toolchain lowered six pins at once, for
  // everybody, because the pins are shared through the repository.
  if (flags.all || flags.harnesses) {
    try {
      // In the SOURCE this upgrade installed from, not in the copy it installed
      // to: the installed one is replaced from git on the next upgrade, so a pin
      // written there is gone by morning and the repository never moves.
      const moved = require("../box").syncPins({ context: pathMod.join(pkg, "box") });
      if (moved.length) {
        console.log(`\nbox image pins updated in ${pkg}:`);
        for (const p of moved) console.log(`  ${p.arg}: ${p.from} → ${p.to}`);
        console.log("  commit them there, then 'broker box build' to rebuild the image");
      }
      // Said out loud rather than passed over: a pin that stayed put
      // because this machine is behind is worth knowing about.
      for (const p of moved.skipped || []) {
        console.log(`  ${p.arg}: kept at ${p.pinned}; this machine has ${p.installed}`);
      }
      } catch (_e) {
      // no box context here — nothing to pin
    }
  }

  if (flags.server) {
    // Use the new source, not modules already loaded by this old CLI.
    execFileSync(process.execPath, [pathMod.join(pkg, "cli.js"), "server", "install", "--no-ask", "--from", pkg], { stdio: "inherit" });
  }
  if (failures) process.exitCode = 1;
};
