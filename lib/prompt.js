const fs = require("node:fs");
const tty = require("node:tty");
const readline = require("node:readline");
const { Writable } = require("node:stream");

// curl | bash can still have a controlling terminal. CI/containers without one
// never wait for input, and --no-ask disables even the /dev/tty fallback.
function openPrompter(noAsk = false) {
  if (noAsk || !process.stderr.isTTY) return null;
  let input = process.stdin;
  let owned = false;
  if (!input.isTTY) {
    try { input = new tty.ReadStream(fs.openSync("/dev/tty", "r")); owned = true; }
    catch (_err) { return null; }
  }
  let muted = false;
  const output = new Writable({ write(chunk, encoding, done) {
    if (!muted) process.stderr.write(chunk, encoding);
    done();
  } });
  output.isTTY = true;
  const rl = readline.createInterface({ input, output, terminal: true });
  let pending;
  let closed = false;
  rl.on("SIGINT", () => rl.close());
  rl.on("close", () => { closed = true; if (pending) { pending(null); pending = null; } });
  return {
    ask(question, { secret = false } = {}) {
      if (closed) return Promise.resolve(null);
      return new Promise((resolve) => {
        pending = resolve;
        if (secret) { process.stderr.write(question); muted = true; }
        rl.question(secret ? "" : question, (answer) => {
          muted = false;
          if (secret) process.stderr.write("\n");
          pending = null;
          resolve(answer.trim());
        });
      });
    },
    close() {
      muted = false;
      rl.close();
      if (owned) input.destroy();
    }
  };
}

module.exports = { openPrompter };
