// What each provider's wrapper is called, where its auth file goes, and which
// launcher it gets. The one table three files read.
//
// homeEnv is the provider's own "config dir" variable: setting it gives an
// account its own profile, so two accounts never fight over one auth file.
const config = require("./config");


const WRAP = {
  codex: { cmd: "broker-cx", bin: "codex", authdir: ".codex", authname: "auth.json", homeEnv: "CODEX_HOME", format: "authjson", template: "codex.py", profileBase: ".codex", authRel: "auth.json", containerShim: ".local/bin" },
  // claude takes its token through CLAUDE_CODE_OAUTH_TOKEN, so it needs no
  // profile and nothing in ~/.claude splits per account — see providers/claude.py.
  claude: { cmd: "broker-cl", bin: "claude", authdir: ".claude", authname: ".credentials.json", homeEnv: "CLAUDE_CONFIG_DIR", format: "authjson", template: "claude.py", profileBase: null, authRel: null, containerShim: ".local/bin",
    // Where its updater puts versions. It installs new ones beside the old and
    // leaves the launcher alone — deliberately, since ours is not its own — so
    // after an update something has to point at what was just installed.
    versions: ".local/share/claude/versions" },
  // agy keeps its token three levels inside $HOME and has no config-dir variable
  // of its own, so its profile IS a home directory (see providers/agy.py). The
  // engine handles that; the entries here only describe the launcher.
  agy: { cmd: "broker-agy", bin: "agy", authdir: ".gemini/antigravity-cli", authname: "antigravity-oauth-token", homeEnv: "HOME", format: "authjson", template: "agy.py", profileBase: ".agy", authRel: ".gemini/antigravity-cli/antigravity-oauth-token", containerShim: null },
  // opencode keeps its own subscription and its own login. The broker holds no
  // credentials for it and chooses no account — this entry exists so that
  // `opencode --box <name>` works, and for nothing else. Anything asked of it
  // without a box goes straight through to the harness as installed.
  opencode: { cmd: "broker-oc", bin: "opencode", homeEnv: "HOME", template: "opencode.py", boxOnly: true, updateCommand: "upgrade" }
};

// The wrapper resolves its account at RUN time, so `broker set-default <name>`
// takes effect without re-wrapping. An account pinned at wrap time (explicit
// `--account`) sits between the env override and the config default.
// The wrapper's own account variable. It is built from the PROVIDER, not the
// command: `broker-cl`.toUpperCase() is BROKER-CL, and `${BROKER-CL_ACCOUNT:-x}`
// is not a variable reference at all — sh parses it as ${BROKER-…}, "use $BROKER
// or this default", so the account silently became the literal `CL_ACCOUNT:-`.
// CLAUDE_ACCOUNT also matches what the python engine and CI already use.

module.exports = { WRAP };
