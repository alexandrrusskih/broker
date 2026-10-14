// The box subcommands: build the image, say what exists, count the slots a
// machine has left, and put a terminal back in order.
"use strict";

// `box connect` is parsed here rather than by the shared flag parser, which
// keeps only the last value of a repeated flag and does not stop at `--`. Both
// matter: a server takes several variables and several roots, and its own argv
// has flags of its own that are none of our business.
function connectSpec(rest) {
  const pathMod = require("path");
  const spec = { env: {}, roots: [], inherit: [], identity: [], lang: "py", out: null, name: null };
  const want = (i, flag) => {
    if (i + 1 >= rest.length) throw new Error(`${flag} needs a value`);
    return rest[i + 1];
  };
  let i = 0;
  for (; i < rest.length; i++) {
    const a = rest[i];
    if (a === "--") { i++; break; }
    if (a === "--out") { spec.out = want(i, a); i++; }
    else if (a === "--lang") { spec.lang = want(i, a); i++; }
    else if (a === "--root") { spec.roots.push(want(i, a)); i++; }
    else if (a === "--inherit") { spec.inherit.push(want(i, a)); i++; }
    else if (a === "--identity") { spec.identity.push(want(i, a)); i++; }
    else if (a === "--env") {
      const pair = want(i, a);
      const at = pair.indexOf("=");
      if (at < 1) throw new Error(`--env takes NAME=VALUE, got '${pair}'`);
      const name = pair.slice(0, at);
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error(`'${name}' is not a variable name`);
      spec.env[name] = pair.slice(at + 1);
      i++;
    } else if (a.startsWith("--")) throw new Error(`unknown flag for box connect: ${a}`);
    else if (spec.name === null) spec.name = a;
    else throw new Error(`box connect takes one server name; put its command after '--'`);
  }
  spec.command = rest.slice(i);

  if (!spec.name) throw new Error("box connect needs the server's name");
  // The name reaches a listener's state file name, so it stays a name.
  if (!/^[A-Za-z0-9._@-]+$/.test(spec.name)) {
    throw new Error(`'${spec.name}' is not a server name — use letters, digits, . _ @ -`);
  }
  if (!spec.out) throw new Error("box connect needs --out <path> for the connector");
  if (!["py", "js"].includes(spec.lang)) throw new Error(`--lang takes 'py' or 'js', not '${spec.lang}'`);
  for (const name of spec.identity) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error(`'${name}' is not a variable name`);
  }
  if (!spec.command.length) throw new Error("box connect needs the server's command after '--'");
  spec.out = pathMod.resolve(spec.out);
  spec.roots = spec.roots.map((r) => pathMod.resolve(r));
  return spec;
}

function box(flags, positional, rest) {
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
    // Another project's Dockerfile is theirs to change, so drift there is said
    // out loud and nothing is written.
    for (const p of (out.pinned || []).foreign || []) {
      console.log(`  pin ${p.arg}: ${p.file} has ${p.from}, this machine ${p.to} — not ours to move`);
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
  } else if (action === "sessions") {
    // Which pane each running box belongs to, and which chat is in it. The
    // pane comes from the launcher, never from the box — a box that could name
    // a pane could be woken in another agent's place. The chat id comes from
    // what the launcher told the harness, or, for a harness that names its own,
    // from the session file it wrote after the box started. Nothing is asked of
    // any container.
    const { execFileSync } = require("child_process");
    const pathMod = require("path");
    const engine = pathMod.join(__dirname, "..", "wrappers");
    const out = execFileSync("python3", ["-c",
      "import sys, json; sys.path.insert(0, sys.argv[1]);" +
      "from broker.box import state;" +
      "print(json.dumps([dict(e, confirmed_by=e.get('confirmed_by') or how)" +
      " for e, how in map(state.resolve, state.live())]))",
      engine], { encoding: "utf8" });
    const boxes = JSON.parse(out);
    if (flags.json) {
      console.log(JSON.stringify(boxes));
    } else if (!boxes.length) {
      console.log("no box is running");
    } else {
      for (const b of boxes) {
        console.log(`${b.pane || "-"}  ${b.harness}  ${b.session || "no id yet"}  ${b.box}` +
          `${b.session ? `  (${b.confirmed_by})` : ""}`);
      }
    }
  } else if (action === "connect") {
    // The same bridge a box gets, for a container that is not a box: the server
    // stays on this machine and only its stdio crosses. Everything that decides
    // WHETHER a listener may be reused — the pane, the workspace, the bus home,
    // the variables and the allowed root — stays on this side; the caller gets a
    // file that speaks MCP on stdin and stdout.
    const { execFileSync } = require("child_process");
    const pathMod = require("path");
    const spec = connectSpec(rest.slice(1));  // rest[0] is the word 'connect'
    const engine = pathMod.join(__dirname, "..", "wrappers");
    // The spec goes in on STDIN, not in argv: a value in `env` can be a secret,
    // and argv is readable by everything on the machine. Prefer --inherit
    // anyway — it names a host variable instead of repeating its value.
    const code = "import json, sys; sys.path.insert(0, sys.argv[1]);" +
      "from broker import box;" +
      "spec = json.load(sys.stdin);" +
      "print(box.connect_mcp(spec['name'], spec['command'], spec['out'], env=spec['env']," +
      " roots=spec['roots'], inherit=spec['inherit'], lang=spec['lang'], identity=spec['identity']))";
    let out;
    try {
      out = execFileSync("python3", ["-c", code, engine],
        { input: JSON.stringify(spec), encoding: "utf8" });
    } catch (e) {
      // A listener that will not start is the one failure a caller has to read,
      // so it arrives as one line rather than as a python traceback.
      const said = String(e.stderr || "").trim().split("\n").pop() || e.message;
      throw new Error(said.replace(/^\w*(Error|Exception):\s*/, ""));
    }
    console.log(out.trim());
  } else if (action === "repair") {
    // For a pane whose harness was killed outright: everything else is
    // handled where the box runs, this is the manual way back.
    const tty = boxes.repairTerminal();
    console.log(tty
      ? "Terminal restored — the keyboard answers normally again."
      : "Not a terminal, nothing to restore.");
  } else {
    throw new Error(`unknown box command '${action}' — use 'build', 'list', 'connect', 'slots', 'sessions' or 'repair'`);
  }
}

module.exports = { box };
