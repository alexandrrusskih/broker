const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");
const color = require("./color");
const config = require("./config");
const shim = require("./shim");
const { install: installWrapper, WRAP } = require("./wrap");

async function ask(question) {
  if (!process.stdin.isTTY) return "";
  const rl = readline.createInterface({ input: process.stdin, output: process.stderr });
  try {
    return await new Promise((resolve) => rl.question(question, resolve));
  } finally {
    rl.close();
  }
}

async function accountsOf(provider) {
  const cfg = config.read();
  if (!cfg.key) return [];
  try {
    const r = await fetch(`${cfg.url}/listAccounts?provider=${encodeURIComponent(provider)}`, {
      headers: { "x-broker-key": cfg.key }
    });
    if (!r.ok) return [];
    return (await r.json()).accounts || [];
  } catch (_e) {
    return [];
  }
}

// Which account this machine belongs to. Asked once at install time, because the
// wrapper stays on it while it has room — that is the whole point: one
// subscription seen from as few places as possible.
async function chooseDefault(provider, preset) {
  if (preset) return config.setAccountFor(provider, preset);
  const existing = config.accountFor(provider);
  if (existing) return existing;

  const accounts = await accountsOf(provider);
  if (!accounts.length) {
    console.log(`  no ${provider} accounts in the broker yet — add one with '${WRAP[provider].cmd} auth <name>'`);
    return null;
  }
  console.log("\n  accounts in the broker: " + accounts.join(", "));
  const answer = (await ask("  which one is yours? (enter to skip) ")).trim();
  if (!answer) return null;
  if (!accounts.includes(answer)) {
    console.log(`  no such account: ${answer} — skipping (set later with 'broker set-default <name>')`);
    return null;
  }
  return config.setAccountFor(provider, answer);
}

async function install(provider, flags) {
  const w = WRAP[provider];
  const r = installWrapper(provider, null, flags["bin-dir"]);
  console.log(`✓ wrapper '${r.cmd}' → ${r.path}`);
  for (const old of r.dropped || []) console.log(`  removed the old name ${old}`);

  if (flags["no-shim"]) {
    console.log(`  shim skipped — plain '${w.bin}' still bypasses the broker`);
  } else {
    const s = shim.install(provider, flags["bin-dir"]);
    console.log(`✓ '${w.bin}' now goes through ${r.cmd} (${s.target})`);
    // Installing the shim is not the same as the shell finding it. A copy from
    // nvm or homebrew earlier in PATH wins, the bare command runs unbrokered, and
    // the first thing it does under a managed account is rotate the token the
    // broker holds — a failure that invalidates the shared grant. Say it loudly here,
    // because this is the moment someone is looking.
    const after = shim.status(provider, flags["bin-dir"]);
    if (after.shadowedBy) {
      console.log(color.red(`\n  ⚠ '${w.bin}' still resolves to ${after.shadowedBy}, NOT the shim.`));
      console.log(color.red(`    A bare '${w.bin}' bypasses the broker and will break the account's token.`));
      console.log(`    Remove that copy, or put ${path.dirname(s.target)} earlier in PATH.`);
      console.log(`    Until then use ${r.cmd} directly.`);
    }
  }

  // Naming the default account IS the non-interactive form: it answers the only
  // question install would ask. --no-ask exists for the upgrade path, which must
  // never block on a prompt.
  const preset = flags["default-account"] || flags.account;
  // --no-ask means "never block on a prompt", not "ignore what I told you".
  // Dropping the preset here would make
  // `broker install agy --default-account account-a --no-ask` record nothing,
  // leaving the account to an unrelated fallback.
  const chosen = preset
    ? config.setAccountFor(provider, String(preset))
    : flags["no-ask"]
      ? config.accountFor(provider)
      : await chooseDefault(provider, null);
  if (chosen) console.log(`✓ your account: ${chosen} (stays on it while it has room)`);

  if (!r.inPath) console.log(`  note: ${path.dirname(r.path)} is not in PATH — add it`);
  console.log(`\n  ${w.bin} …           runs through the broker now`);
  console.log(`  ${r.cmd} list        who is seeded and what is left`);
  console.log(`  broker ${provider} remove   undo the shim`);
}

function remove(provider) {
  const out = shim.remove(provider);
  if (!out.removed) {
    console.log(`${WRAP[provider].bin} is not shimmed — nothing to undo`);
    return;
  }
  console.log(`✓ ${out.target} restored → ${out.original}`);
}

// Green: working. Yellow: works, but something is weaker than it should be —
// the broker is reachable only under its own name, or the machine has no account
// of its own. Red: this provider does not go through the broker at all.
async function status(provider) {
  const s = shim.status(provider);
  const mine = config.accountFor(provider);
  const accounts = await accountsOf(provider);

  console.log(color.bold(provider));

  console.log(
    s.wrapper
      ? color.line("ok", "wrapper", s.wrapper)
      : color.line("bad", "wrapper", `not installed — run 'broker ${provider} install'`)
  );

  if (s.shadowedBy) {
    // The shim is in place and useless: the shell finds another copy first, so a
    // bare `codex` runs unbrokered — and an unbrokered run under a managed account
    // rotates its token and breaks the broker's copy.
    console.log(color.line("bad", "shim", `SHADOWED — '${s.bin}' resolves to ${s.shadowedBy}, not the shim`));
    console.log(color.line("bad", "", `bare '${s.bin}' bypasses the broker and will break the account's token`));
    console.log(color.line("warn", "", `remove that copy, or put ${path.dirname(s.native)} earlier in PATH`));
  } else if (s.shimmed) {
    console.log(color.line("ok", "shim", `${s.native} → ${s.cmd}`));
  } else if (s.broken) {
    // Installed once and gone since: an updater put its own binary back, so the
    // native name silently stopped going through the broker.
    console.log(
      color.line("bad", "shim", `MISSING — an updater took ${s.native} back; run 'broker ${provider} install'`)
    );
  } else {
    // Never installed: `broker-xx` still works, plain `xx` does not — a weaker
    // setup, not a broken one.
    console.log(
      color.line("warn", "shim", `not installed — plain '${s.bin}' bypasses the broker`)
    );
  }

  console.log(
    accounts.length
      ? color.line("ok", "accounts", accounts.join(", "))
      : color.line("bad", "accounts", `none seeded — run '${WRAP[provider].cmd} auth <name>'`)
  );

  console.log(
    mine
      ? color.line("ok", "yours", mine)
      : color.line(
          "warn",
          "yours",
          "not set — every run picks whoever has room; 'broker set-default <name>' pins one"
        )
  );
}

module.exports = { install, remove, status, accountsOf };
