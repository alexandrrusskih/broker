// What every box test needs: a temporary directory that cleans itself up, and a
// way to run the engine and read what it printed. Kept in one file because the
// box tests are split by subject, and eleven copies of this drifted apart twice.
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const root = path.join(__dirname, "..");

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "broker-box-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  // realpath: on macOS the temp directory sits under /var, which is itself a
  // symlink to /private/var — and the code under test resolves symlinks.
  return fs.realpath(dir);
}

// The engine builds the command line; running python is how we see it.
function engine(code, env = {}) {
  return execFileSync("python3", ["-c", `import sys; sys.path.insert(0, 'lib/wrappers')\n${code}`],
    { cwd: root, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", ...env } });
}

// A stand-in for the terminal manager, answering the way the real one does: it
// says ok whether or not it applied a report, so only reading the pane back
// settles anything. Shared because two files ask it different questions.
const MANAGER = `
import json, os, socket, threading
heard = []
held = {}
ready = threading.Event()
def listen(path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    # AFTER listen, not after bind: a client that connects in between is
    # refused, and the test then fails on a socket error rather than on what it
    # is about. Waiting for the path to appear was that race.
    ready.set()
    while True:
        conn, _ = server.accept()
        line = b""
        while b"\\n" not in line:
            chunk = conn.recv(4096)
            if not chunk:
                break
            line += chunk
        request = json.loads(line.split(b"\\n", 1)[0] or b"{}")
        heard.append(request)
        if request.get("method") == "pane.report_agent":
            # The real manager answers ok whether or not it applied the report,
            # so the stand-in does too, and records what it would hold.
            if not held or request["params"].get("source") == held.get("source"):
                held.clear()
                held.update(agent=request["params"].get("agent"), source=request["params"].get("source"),
                            value=request["params"].get("agent_session_id"))
            answer = {"id": request.get("id"), "result": {"type": "ok"}}
        elif request.get("method") == "pane.get":
            answer = {"id": request.get("id"), "result": {"type": "pane_info", "pane": {
                "pane_id": request["params"].get("pane_id"), "agent": held.get("agent"),
                "agent_session": {"agent": held.get("agent"), "kind": "id",
                                  "source": held.get("source"), "value": held.get("value")}}}}
        else:
            answer = {"id": request.get("id"),
                      "error": {"code": "session_not_accepted", "message": "session_not_accepted"}}
        conn.sendall((json.dumps(answer) + "\\n").encode())
        conn.close()
`;

module.exports = { root, temp, engine, MANAGER };
