const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

function sourcePackage(cfg = {}, from) {
  if (from) {
    const local = path.resolve(from);
    if (!fs.existsSync(path.join(local, "package.json"))) throw new Error(`no package.json in ${local}`);
    return local;
  }
  const repo = cfg.src_repo || "git@github.com:alexandrrusskih/broker.git";
  const src = path.join(os.homedir(), ".cache", "broker", "src");
  // DROP AFTER 2026-12. Only a checkout cache — re-cloned on demand, so the old
  // one is simply removed rather than carried across.
  fs.rmSync(path.join(os.homedir(), ".cache", "hltm-broker"), { recursive: true, force: true });
  const git = (...args) => execFileSync("git", args, { stdio: "inherit" });
  if (fs.existsSync(path.join(src, ".git"))) {
    // Updating the dedicated install cache must honor a changed src_repo too.
    const remote = execFileSync("git", ["-C", src, "remote", "get-url", "origin"], { encoding: "utf8" }).trim();
    if (remote !== repo) git("-C", src, "remote", "set-url", "origin", repo);
    git("-C", src, "fetch", "--depth", "1", "origin", "HEAD");
    git("-C", src, "reset", "--hard", "FETCH_HEAD");
  } else {
    fs.mkdirSync(path.dirname(src), { recursive: true });
    git("clone", "--depth", "1", repo, src);
  }
  const pkg = cfg.src_subdir ? path.join(src, cfg.src_subdir) : src;
  if (!fs.existsSync(path.join(pkg, "package.json"))) throw new Error(`no package.json in ${pkg}`);
  return pkg;
}

module.exports = { sourcePackage };
