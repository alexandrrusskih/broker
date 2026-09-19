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

function build(options = {}) {
  const runtime = options.runtime || "docker";
  const args = ["build", "-t", options.image || IMAGE];
  // Pinning is a deliberate act; the default follows whatever is current when
  // you build, which is also when you decide to take a new version.
  if (options.claude) args.push("--build-arg", `CLAUDE_VERSION=${options.claude}`);
  if (options.codex) args.push("--build-arg", `CODEX_VERSION=${options.codex}`);
  if (options.noCache) args.push("--no-cache");
  args.push(CONTEXT);
  execFileSync(runtime, args, { stdio: "inherit" });
  return { image: options.image || IMAGE, context: CONTEXT };
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

module.exports = { build, list, profiles, seedProfiles, IMAGE, PROFILES, CONTEXT };
