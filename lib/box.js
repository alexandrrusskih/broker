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
    built.push({ name, tag, file });
  }
  return built;
}

// What each harness reports on this machine, and which build arg pins it.
// A box exists to reproduce this machine, so the image should carry what the
// machine carries — not whatever the registry called "latest" the day it was
// built. `broker upgrade` updates these after it updates the harnesses.
const PINS = {
  CLAUDE_VERSION: { cmd: "claude", args: ["--version"] },
  CODEX_VERSION: { cmd: "codex", args: ["--version"] },
  AGY_VERSION: { cmd: "agy", args: ["--version"] },
  BUN_VERSION: { cmd: "bun", args: ["--version"] },
  RTK_VERSION: { cmd: "rtk", args: ["--version"] },
  GH_VERSION: { cmd: "gh", args: ["--version"] },
  GLAB_VERSION: { cmd: "glab", args: ["--version"] },
};

function installedVersion(spec) {
  try {
    const out = execFileSync(spec.cmd, spec.args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    const found = out.match(/\d+\.\d+\.\d+/);
    return found ? found[0] : null;
  } catch (_e) {
    return null; // not installed here; leave the pin alone
  }
}

function syncPins() {
  const dockerfile = path.join(CONTEXT, "Dockerfile");
  let text;
  try {
    text = fs.readFileSync(dockerfile, "utf8");
  } catch (_e) {
    return [];
  }
  const changed = [];
  for (const [arg, spec] of Object.entries(PINS)) {
    const version = installedVersion(spec);
    if (!version) continue;
    const pattern = new RegExp(`^(ARG ${arg}=)(\\S+)$`, "m");
    const current = text.match(pattern);
    if (!current || current[2] === version) continue;
    text = text.replace(pattern, `$1${version}`);
    changed.push({ arg, from: current[2], to: version });
  }
  if (changed.length) fs.writeFileSync(dockerfile, text);
  return changed;
}

function build(options = {}) {
  const runtime = options.runtime || "docker";
  const args = ["build", "-t", options.image || IMAGE];
  // Pinning is a deliberate act; the default follows whatever is current when
  // you build, which is also when you decide to take a new version.
  if (options.claude) args.push("--build-arg", `CLAUDE_VERSION=${options.claude}`);
  if (options.codex) args.push("--build-arg", `CODEX_VERSION=${options.codex}`);
  if (options.bun) args.push("--build-arg", `BUN_VERSION=${options.bun}`);
  if (options.noCache) args.push("--no-cache");
  args.push(CONTEXT);
  execFileSync(runtime, args, { stdio: "inherit" });
  // Boxes that extend it are rebuilt too, or they keep a base that no longer exists.
  const extended = options.baseOnly ? [] : buildBoxes({ runtime, only: options.only });
  return { image: options.image || IMAGE, context: CONTEXT, extended };
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

module.exports = { build, buildBoxes, imageFor, list, profiles, seedProfiles, syncPins, IMAGE, PROFILES, CONTEXT };
