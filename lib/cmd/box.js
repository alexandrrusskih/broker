// The box subcommands: build the image, say what exists, count the slots a
// machine has left, and put a terminal back in order.
"use strict";

function box(flags, positional) {
  const boxes = require("../box");
  const action = positional[0] || "list";
  if (action === "build") {
    // --from <checkout>: build the Dockerfile THERE. Without it the image comes
    // from the box directory of whichever broker is running, which for an
    // installed copy is the version that was last installed.
    const pathMod = require("path");
    const from = typeof flags.from === "string"
      ? pathMod.join(pathMod.resolve(flags.from), "box") : null;
    const out = boxes.build({
      image: flags.image, claude: flags.claude, codex: flags.codex, bun: flags.bun,
      noCache: flags["no-cache"] === true, runtime: flags.runtime, context: from
    });
    console.log(`Image ready: ${out.image}`);
    console.log(`  built from ${out.context}`);
    for (const p of out.pinned || []) console.log(`  pin ${p.arg}: ${p.from} → ${p.to}`);
    for (const p of (out.pinned || []).skipped || []) {
      console.log(`  pin ${p.arg}: kept at ${p.pinned}; this machine has ${p.installed}`);
    }
    for (const extra of out.extended || []) {
      console.log(`  + ${extra.tag}  (${extra.name}, from ${extra.file})`);
    }
    if (boxes.seedProfiles()) console.log(`Wrote an example box file: ${boxes.PROFILES}`);
    console.log(`Define boxes in ${boxes.PROFILES}, then: claude --box <name>`);
  } else if (action === "list" || action === "ls") {
    const out = boxes.list();
    console.log(`file    ${out.file}`);
    console.log(`image   ${out.image ? `${boxes.IMAGE} (built)` : `${boxes.IMAGE} — not built, run 'broker box build'`}`);
    if (!out.runtime) console.log("docker  not reachable — start Docker Desktop");
    if (!out.boxes) {
      console.log("\nNo boxes defined yet. 'broker box build' writes an example file.");
      return;
    }
    for (const [name, box] of Object.entries(out.boxes)) {
      const rw = (box.rw || []).map((e) => (typeof e === "string" ? e : `${e.source} → ${e.target}`)).join(", ") || "—";
      const ro = (box.ro || []).length ? `  ro: ${box.ro.join(", ")}` : "";
      const image = box.image || (box.dockerfile ? boxes.imageFor(name) : boxes.IMAGE);
      console.log(`\n${name}\n  image: ${image}${box.dockerfile ? `  ← ${box.dockerfile}` : ""}\n  rw: ${rw}${ro ? "\n" + ro : ""}`);
    }
  } else if (action === "slots") {
    // How much room this machine has left for heavy work. The ceiling is
    // per MACHINE — thirteen boxes with two slots each would be no ceiling
    // at all — so a person deciding what to start next cannot tell from
    // inside their own box, and twelve agents queueing on two slots wait
    // minutes without knowing why.
    const { execFileSync } = require("child_process");
    const osMod = require("os");
    const pathMod = require("path");
    const dir = positional[1] || pathMod.join(osMod.homedir(), ".cache", "broker-box", "slots");
    const engine = pathMod.join(__dirname, "..", "wrappers");
    const out = execFileSync("python3", ["-c",
      "import sys, json; sys.path.insert(0, sys.argv[1]);" +
      "from broker.box import slots;" +
      "free, total, busy = slots.state(sys.argv[2]);" +
      "print(json.dumps({'free': free, 'total': total, 'busy': " +
      "[{'slot': b, 'owner': slots.owner(sys.argv[2], b)} for b in busy]}))",
      engine, dir], { encoding: "utf8" });
    const seen = JSON.parse(out);
    if (!seen.total) {
      console.log(`no slot files in ${dir}`);
    } else if (flags.json) {
      console.log(JSON.stringify(seen));
    } else {
      console.log(`${seen.free} of ${seen.total} free`);
      for (const b of seen.busy) console.log(`  ${b.slot} busy${b.owner ? ` — ${b.owner}` : ""}`);
    }
  } else if (action === "repair") {
    // For a pane whose harness was killed outright: everything else is
    // handled where the box runs, this is the manual way back.
    const tty = boxes.repairTerminal();
    console.log(tty
      ? "Terminal restored — the keyboard answers normally again."
      : "Not a terminal, nothing to restore.");
  } else {
    throw new Error(`unknown box command '${action}' — use 'build', 'list' or 'repair'`);
  }
}

module.exports = { box };
