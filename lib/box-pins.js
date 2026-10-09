// The versions a box image is pinned to, and the rule that moves them.
//
// A pin is shared through the repository, while what is installed belongs to
// one machine — so a pin only ever moves forward.
const fs = require("fs");
const { execFileSync } = require("child_process");


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
  BUILDX_VERSION: { cmd: "docker", args: ["buildx", "version"] },
  COMPOSE_VERSION: { cmd: "docker", args: ["compose", "version"] },
  // --no-install so a machine without Playwright reports nothing instead of
  // downloading it just to be asked its version.
  PLAYWRIGHT_VERSION: { cmd: "npx", args: ["--no-install", "playwright", "--version"] },
  OPENCODE_VERSION: { cmd: "opencode", args: ["--version"] },
  GITLEAKS_VERSION: { cmd: "gitleaks", args: ["version"] },
  // Not in the base image: only the boxes that touch infrastructure carry it.
  // Kept level with the machine's own on purpose — OpenTofu records in a state
  // file which version last wrote it, and a newer one writing makes the older
  // refuse to read it afterwards, putting the host's infrastructure out of
  // reach of the host.
  TOFU_VERSION: { cmd: "tofu", args: ["--version"] },
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

// Which of two versions is the later one. Numeric parts compared as numbers,
// so 1.116.0 beats 1.99.0; a part that is not a number (a "v" prefix, a build
// suffix) falls back to comparing text, and anything unrecognisable returns
// null rather than guessing.
function laterVersion(a, b) {
  const parts = (v) => String(v).replace(/^v/, "").split(/[.\-+]/);
  const left = parts(a);
  const right = parts(b);
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    const x = left[i] ?? "0";
    const y = right[i] ?? "0";
    if (x === y) continue;
    const nx = Number(x);
    const ny = Number(y);
    if (Number.isNaN(nx) || Number.isNaN(ny)) return x > y ? a : b;
    return nx > ny ? a : b;
  }
  return a;
}


function syncPinsIn(dockerfile, options, changed, skipped) {
  let text;
  try {
    text = fs.readFileSync(dockerfile, "utf8");
  } catch (error) {
    // A box may name a Dockerfile that is not there; anything else is a fault
    // here, and swallowing it once hid a missing require for a whole release.
    if (error.code === "ENOENT") return;
    throw error;
  }
  const before = text;
  for (const [arg, spec] of Object.entries(PINS)) {
    // The reader is replaceable so that a test can say what this machine has.
    const version = (options.installed || installedVersion)(spec, arg);
    if (!version) continue;
    const pattern = new RegExp(`^(ARG ${arg}=)(\\S+)$`, "m");
    const current = text.match(pattern);
    if (!current || current[2].replace(/^v/, "") === version.replace(/^v/, "")) continue;
    // Pins are shared through the repository, while what is installed belongs
    // to one machine. Whoever ran this with an older toolchain used to lower
    // the pin for everybody — a colleague's `upgrade` rewrote six of them
    // downwards at once. A pin therefore only ever moves forward, unless the
    // downgrade is asked for by name.
    if (!options.allowDowngrade && laterVersion(current[2], version) === current[2]) {
      skipped.push({ arg, pinned: current[2], installed: version });
      continue;
    }
    // Keep the shape of the pin that is there. The buildx pin is a git TAG —
    // v0.38.0 — and the Dockerfile interpolates it straight into a release URL
    // and an asset name, so the bare number would make the next image build 404.
    const shaped = current[2].startsWith("v") && !version.startsWith("v")
      ? "v" + version : version;
    text = text.replace(pattern, `$1${shaped}`);
    changed.push({ arg, from: current[2], to: shaped });
  }
  if (text !== before) fs.writeFileSync(dockerfile, text);
}

module.exports = { PINS, installedVersion, laterVersion, syncPinsIn };
