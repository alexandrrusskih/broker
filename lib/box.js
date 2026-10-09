// Building the image a --box runs in, and saying what boxes exist.
//
// Running one is the engine's job (lib/wrappers/broker/box.py): by then an
// account has been picked and its token resolved, and that is what gets handed
// in. This side only deals with the image and the profile file.
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");
const config = require("./config");
const pins = require("./box-pins");

const IMAGE = "broker-box";
const PROFILES = path.join(path.dirname(config.FILE), "boxes.json");
const CONTEXT = path.join(__dirname, "..", "box");

const EXAMPLE = `{
  // A box is a name and the directories it may touch. Each is mounted at the
  // SAME path inside, so session history and --resume keep working.
  //
  //   claude --box work            interactive, in this box
  //   codex --box work exec "..."  the same box, the other harness
  //
  // The harness's own directory (~/.claude, ~/.codex) comes along on its own —
  // settings, MCP servers, agents, history. Do not list it here.
  "work": {
    "rw": ["~/Projects/example"],
    "ro": ["~/Projects/reference"]
  }
}
`;

// Comments are the point of keeping this file by hand, so they are stripped
// before parsing rather than forbidden. Mirrors _strip_comments in box.py.
// Undoing what a harness did to this terminal, for the one case the box itself
// cannot cover. box/run.py restores the terminal on every ending it can see —
// a clean exit, a crash, `docker stop`, SIGTERM — but nothing survives SIGKILL,
// and a pane left in the harness's modes answers the keyboard in a language the
// shell does not speak: Enter arrives as "27;3u", an arrow as "1:1A".
//
// Kept in step with TERMINAL_RESET in lib/wrappers/broker/box/run.py.
const TERMINAL_RESET =
  "\x1b[?1049l" +      // leave the alternate screen
  "\x1b[<u" +          // pop the kitty keyboard flags
  "\x1b[=0;1u" +       // ...and clear any set outright
  "\x1b[?1l\x1b>" +    // cursor keys and keypad back to normal
  "\x1b[?2004l" +      // bracketed paste off
  "\x1b[?1004l" +      // focus reporting off — it arrives as "\x1b[O"
  "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l" +  // mouse off
  "\x1b[?25h" +        // cursor visible
  "\x1b[0m";           // attributes back to default

function repairTerminal() {
  if (process.stdout.isTTY) process.stdout.write(TERMINAL_RESET);
  // The emulator's modes are only half of it: the driver may still be in raw
  // mode with echo off, which is what makes a pane look dead rather than noisy.
  try {
    require("child_process").execFileSync("stty", ["sane"], {
      stdio: ["inherit", "ignore", "ignore"],
    });
  } catch {
    // No tty to speak of (piped output, a CI log) — the escapes above were
    // already skipped, and there is nothing else to put back.
  }
  return process.stdout.isTTY;
}

function stripComments(text) {
  let out = "";
  for (let i = 0; i < text.length;) {
    if (text[i] === '"') {
      let j = i + 1;
      while (j < text.length && (text[j] !== '"' || text[j - 1] === "\\")) j++;
      out += text.slice(i, j + 1);
      i = j + 1;
    } else if (text.startsWith("//", i)) {
      const end = text.indexOf("\n", i);
      if (end < 0) break;
      i = end;
    } else if (text.startsWith("/*", i)) {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 2;
    } else {
      out += text[i];
      i++;
    }
  }
  return out;
}

function profiles() {
  try {
    return JSON.parse(stripComments(fs.readFileSync(PROFILES, "utf8")));
  } catch (err) {
    if (err.code === "ENOENT") return null;
    throw new Error(`unreadable ${PROFILES}: ${err.message}`);
  }
}

// Written only when absent, and never rewritten: this file is yours to edit.
function seedProfiles() {
  if (fs.existsSync(PROFILES)) return false;
  fs.mkdirSync(path.dirname(PROFILES), { recursive: true, mode: 0o700 });
  fs.writeFileSync(PROFILES, EXAMPLE, { mode: 0o600, flag: "wx" });
  return true;
}

// A box that needs more than the base image names its own Dockerfile, which
// starts FROM broker-box. Keeps the base thin: a rust toolchain for one project
// and browsers for another would quadruple an image forty projects share.
function imageFor(name) {
  return `${IMAGE}-${String(name).replace(/[^a-zA-Z0-9_.-]/g, "-").toLowerCase()}`;
}

function buildBoxes(options = {}) {
  const runtime = options.runtime || "docker";
  const defined = profiles() || {};
  const built = [];
  for (const [name, box] of Object.entries(defined)) {
    if (!box || !box.dockerfile || (options.only && options.only !== name)) continue;
    const file = path.resolve(box.dockerfile.replace(/^~(?=$|\/)/, os.homedir()));
    if (!fs.existsSync(file)) throw new Error(`the '${name}' box names a Dockerfile that is not there: ${file}`);
    const tag = box.image || imageFor(name);
    // Built from its own directory, so the Dockerfile can COPY what sits beside it.
    execFileSync(runtime, ["build", "-t", tag, "-f", file, path.dirname(file)], { stdio: "inherit" });
    if (box.imageArchive) {
      const archive = path.resolve(box.imageArchive.replace(/^~(?=$|\/)/, os.homedir()));
      fs.mkdirSync(path.dirname(archive), { recursive: true });
      const pending = `${archive}.tmp-${process.pid}`;
      try {
        execFileSync(runtime, ["save", "--output", pending, tag], { stdio: "inherit" });
        // Docker Desktop can report an OCI index ID here. A box's older DinD
        // loads the platform image instead, whose ID is the archive Config.
        const manifest = JSON.parse(execFileSync("tar", ["-xOf", pending, "manifest.json"],
          { encoding: "utf8" }));
        const configPath = manifest.length === 1 && manifest[0].Config;
        const configId = typeof configPath === "string" &&
          configPath.match(/(?:^|\/)sha256\/([0-9a-f]{64})$/);
        if (!configId) throw new Error(`invalid Docker image archive: ${archive}`);
        const imageId = `sha256:${configId[1]}`;
        fs.renameSync(pending, archive);
        fs.writeFileSync(`${archive}.id.tmp-${process.pid}`, `${imageId}\n`);
        fs.renameSync(`${archive}.id.tmp-${process.pid}`, `${archive}.id`);
      } finally {
        fs.rmSync(pending, { force: true });
        fs.rmSync(`${archive}.id.tmp-${process.pid}`, { force: true });
      }
    }
    built.push({ name, tag, file });
  }
  return built;
}

// Every Dockerfile a pin could appear in: the base, and the ones boxes bring of
// their own. A tool that moved out of the base into one box must keep moving
// with the machine, or the box that needs it most is the one left behind.
function pinnedFiles(options = {}) {
  // Which checkout's Dockerfile. CONTEXT is relative to the file being run, so
  // an installed copy would pin ITS OWN box directory — which the next upgrade
  // replaces from git, losing the pin and leaving the repository behind. The
  // caller says where the source is; see lib/cmd/upgrade.js.
  const files = [path.join(options.context || CONTEXT, "Dockerfile")];
  for (const box of Object.values(profiles() || {})) {
    if (!box || !box.dockerfile) continue;
    const file = path.resolve(box.dockerfile.replace(/^~(?=$|\/)/, os.homedir()));
    if (!files.includes(file) && fs.existsSync(file)) files.push(file);
  }
  return files;
}

function syncPins(options = {}) {
  const changed = [];
  const skipped = [];
  // Only inside the checkout being built. A box may name a Dockerfile that
  // belongs to ANOTHER project, and that file is theirs: finik pins Playwright
  // to the version its own package.json depends on, says so in a comment, and
  // a browser build it did not expect makes the harness refuse to start. This
  // sync wrote 1.63.0 -> 1.64.0 in their tracked file and broke their
  // preflight on a dirty tree. A build mirrors the machine into the pins of
  // the checkout it builds, and reports drift elsewhere instead of writing it.
  const root = path.dirname(options.context || CONTEXT) + path.sep;
  const foreign = [];
  for (const dockerfile of pinnedFiles(options)) {
    if (!dockerfile.startsWith(root)) {
      const seen = [];
      pins.syncPinsIn(dockerfile, { ...options, dryRun: true }, seen, []);
      for (const p of seen) foreign.push({ ...p, file: dockerfile });
      continue;
    }
    pins.syncPinsIn(dockerfile, options, changed, skipped);
  }
  changed.skipped = skipped;
  changed.foreign = foreign;
  return changed;
}

function build(options = {}) {
  const runtime = options.runtime || "docker";
  // Which checkout's box directory. CONTEXT is relative to the file being run,
  // so an installed copy builds ITS OWN Dockerfile — which is whatever version
  // was last installed, not what the repository says. That is how three images
  // came out without the agntbus binary the repository had already added. The
  // caller may name the source; `broker box build` prints what it used.
  const context = options.context || CONTEXT;
  if (!fs.existsSync(path.join(context, "Dockerfile"))) {
    throw new Error(`no Dockerfile in ${context}`);
  }
  // A box exists to reproduce THIS machine, so the image is always built
  // against what the machine has now — not against whatever was pinned the last
  // time somebody remembered to sync. Pins still only move FORWARD, so building
  // on a machine that is behind lowers nothing; it says what it kept instead.
  const pinned = syncPins({ context });
  const args = ["build", "-t", options.image || IMAGE];
  // An explicit version overrides the pin for this one build, and is not
  // written down: naming it is a deliberate act, taking it is a separate one.
  if (options.claude) args.push("--build-arg", `CLAUDE_VERSION=${options.claude}`);
  if (options.codex) args.push("--build-arg", `CODEX_VERSION=${options.codex}`);
  if (options.bun) args.push("--build-arg", `BUN_VERSION=${options.bun}`);
  if (options.noCache) args.push("--no-cache");
  args.push(context);
  execFileSync(runtime, args, { stdio: "inherit" });
  // Boxes that extend it are rebuilt too, or they keep a base that no longer exists.
  const extended = options.baseOnly ? [] : buildBoxes({ runtime, only: options.only });
  return { image: options.image || IMAGE, context, extended, pinned };
}

function list() {
  const defined = profiles();
  const runtime = (() => {
    try { execFileSync("docker", ["info", "--format", "{{.ServerVersion}}"], { stdio: ["ignore", "pipe", "ignore"] }); return true; }
    catch (_e) { return false; }
  })();
  const image = (() => {
    try {
      return execFileSync("docker", ["image", "inspect", IMAGE, "--format", "{{.Id}}"], { stdio: ["ignore", "pipe", "ignore"], encoding: "utf8" }).trim();
    } catch (_e) { return null; }
  })();
  return { file: PROFILES, boxes: defined, runtime, image, home: os.homedir() };
}

module.exports = { laterVersion: pins.laterVersion, repairTerminal, build, buildBoxes, imageFor, list, profiles, seedProfiles, syncPins, IMAGE, PROFILES, CONTEXT };
