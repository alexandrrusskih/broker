// Colour for status output, and nothing else.
//
// Three states, because that is what a status line actually answers: it works
// (green), it works but something is off (yellow), it is broken (red). Anything
// finer would need a legend, and a legend means the colours failed.
//
// Off when the output is not a terminal (a pipe or a CI log keeps the escape
// codes as literal junk), when NO_COLOR is set (the informal standard), or when
// TERM says dumb. FORCE_COLOR overrides all of that.
const ON = (() => {
  if (process.env.FORCE_COLOR) return true;
  if (process.env.NO_COLOR !== undefined) return false;
  if (process.env.TERM === "dumb") return false;
  return Boolean(process.stdout.isTTY);
})();

const CODES = { green: 32, yellow: 33, red: 31, dim: 2, bold: 1 };

function paint(name, text) {
  if (!ON || !CODES[name]) return String(text);
  return `[${CODES[name]}m${text}[0m`;
}

const green = (t) => paint("green", t);
const yellow = (t) => paint("yellow", t);
const red = (t) => paint("red", t);
const dim = (t) => paint("dim", t);
const bold = (t) => paint("bold", t);

// A status line: its mark and its text carry the same state, so the line reads
// the same to someone who cannot tell the colours apart.
const MARKS = { ok: "✓", warn: "!", bad: "✗" };
const PAINT = { ok: green, warn: yellow, bad: red };

function line(state, label, text) {
  const mark = PAINT[state](MARKS[state]);
  return `  ${mark} ${label.padEnd(9)} ${PAINT[state](text)}`;
}

module.exports = { green, yellow, red, dim, bold, line, enabled: ON };
