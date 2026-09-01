const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync, execSync } = require("child_process");
const config = require("./config");

// Package root — where functions/, firebase.json, firestore.* live.
const PKG_ROOT = path.join(__dirname, "..");

function run(cmd, args, opts = {}) {
  return execFileSync(cmd, args, { stdio: "inherit", ...opts });
}

function firebase(args, opts = {}) {
  // Prefer a global firebase; otherwise fall back to npx (zero install).
  try {
    execSync("firebase --version", { stdio: "ignore" });
    return run("firebase", args, opts);
  } catch (_e) {
    return run("npx", ["-y", "firebase-tools", ...args], opts);
  }
}

function firebaseCapture(args) {
  try {
    return execFileSync("firebase", args, { encoding: "utf8" });
  } catch (_e) {
    return execFileSync("npx", ["-y", "firebase-tools", ...args], { encoding: "utf8" });
  }
}

// Stage the package's server files into a temp dir so `firebase deploy` runs
// against a clean, self-contained project pinned to the target Firebase project.
function stage(project) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "hltm-broker-deploy-"));
  fs.cpSync(path.join(PKG_ROOT, "functions"), path.join(dir, "functions"), { recursive: true });
  fs.copyFileSync(path.join(PKG_ROOT, "firebase.json"), path.join(dir, "firebase.json"));
  fs.copyFileSync(path.join(PKG_ROOT, "firestore.rules"), path.join(dir, "firestore.rules"));
  fs.copyFileSync(path.join(PKG_ROOT, "firestore.indexes.json"), path.join(dir, "firestore.indexes.json"));
  fs.writeFileSync(path.join(dir, ".firebaserc"), JSON.stringify({ projects: { default: project } }, null, 2));
  return dir;
}

async function deploy({ project, alertWebhook, dedicatedProject }) {
  if (!project) throw new Error("--project <firebase-project-id> is required");
  if (!dedicatedProject) {
    throw new Error(
      "refusing to replace Firestore rules in a shared project; use a dedicated Firebase project and pass --dedicated-project"
    );
  }

  // 1) ensure logged in (interactive only if needed)
  let loggedIn = false;
  try {
    const who = firebaseCapture(["login:list"]);
    loggedIn = !/No authorized accounts/i.test(who) && /\S+@\S+/.test(who);
  } catch (_e) {
    loggedIn = false;
  }
  if (!loggedIn) {
    console.log("→ firebase login (one-time, opens a browser)...");
    firebase(["login"]);
  }

  // 2) stage + provision a one-time Secret Manager bootstrap token + deploy.
  // The token file never enters the function source tree and is removed with the
  // staging directory whether deployment succeeds or fails.
  const dir = stage(project);
  const bootstrapToken = require("crypto").randomBytes(32).toString("hex");
  const bootstrapFile = path.join(dir, ".bootstrap-token");
  fs.writeFileSync(bootstrapFile, bootstrapToken, { mode: 0o600 });
  try {
    console.log(`→ provisioning one-time bootstrap secret...`);
    firebase(
      ["functions:secrets:set", "BROKER_BOOTSTRAP_TOKEN", "--data-file", bootstrapFile, "--project", project],
      { cwd: dir }
    );

    console.log(`→ installing function deps...`);
    run("npm", ["install", "--omit=dev", "--no-audit", "--no-fund"], { cwd: path.join(dir, "functions") });

    console.log(`→ deploying the broker function + deny-all firestore rules to ${project}...`);
    // Naming the function avoids treating unrelated functions as deletions. The
    // acknowledgement above makes replacing the database's entire client
    // ruleset an explicit dedicated-project operation.
    firebase(["deploy", "--only", "functions:broker,firestore:rules", "--project", project, "--force"], { cwd: dir });

    // 3) compute base url (gen2 default region us-central1)
    const url = `https://us-central1-${project}.cloudfunctions.net/broker`;

    // 4) bootstrap over the authenticated one-time channel. Firestore then
    // atomically closes bootstrap forever by storing the broker key.
    console.log("→ bootstrapping broker key...");
    const resp = await fetch(`${url}/bootstrap`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-bootstrap-token": bootstrapToken },
      body: JSON.stringify(alertWebhook ? { alert_webhook: alertWebhook } : {})
    });
    const body = await resp.json().catch(() => ({}));
    if (resp.status === 403) {
      const existing = config.read();
      if (!existing.key) {
        throw new Error("already bootstrapped on the server but no local key — run `broker config --url " + url + " --key <key>`");
      }
      config.write({ url, project });
    } else if (resp.status === 200 && body.broker_key) {
      config.write({ url, key: body.broker_key, project });
    } else {
      throw new Error(`bootstrap failed ${resp.status}: ${JSON.stringify(body).slice(0, 160)}`);
    }

    return { url, project };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

module.exports = { deploy };
