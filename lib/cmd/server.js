// Running a broker of your own: provisioning the files, serving them, and the
// system daemon that keeps a server up.
"use strict";

const config = require("../config");

async function manage(flags, positional) {
  const manager = require("../service").createServiceManager();
  const action = positional[0] || "status";
  if (action === "install") {
    const out = await manager.install({
      url: flags.url, dataDir: flags["data-dir"], port: flags.port,
      from: flags.from, noAsk: flags["no-ask"] === true
    });
    console.log(`System broker ${out.updated ? "updated" : "installed"} and ready on 127.0.0.1:${out.port}`);
    console.log(`Runtime: ${out.runtime}\nData: ${out.dataDir}`);
    console.log(`Client config: ${out.clientFile}\nAdmin config: ${out.adminFile} (keep on the server)`);
    console.log(`Client URL: ${out.url}`);
    console.log(`Private HTTPS is separate: tailscale serve --bg http://127.0.0.1:${out.port}`);
    console.log("No client shim, Tailscale setting, old refresh daemon or existing Codex auth was changed.");
  } else if (action === "status") {
    const out = await manager.status();
    console.log(`system/${require("../service").LABEL}: ${out.healthy ? "ready" : out.pid ? "running, API not ready" : "stopped"}`);
    console.log(`User: ${out.user}\nRuntime: ${out.runtime}\nData: ${out.dataDir}\nClient URL: ${out.url}`);
    if (!out.healthy) process.exitCode = 1;
  } else if (action === "restart") {
    await manager.restart();
    console.log("System broker restarted and ready.");
  } else if (action === "stop") {
    await manager.stop();
    console.log("System broker stopped. Use broker server restart to start it again; its boot-time registration is retained.");
  } else throw new Error("usage: broker server install|status|restart|stop");
}

async function init(flags) {
  const out = await require("../server").init({ dataDir: flags["data-dir"], url: flags.url });
  console.log(`Self-hosted broker initialized: ${out.dataDir}`);
  console.log(`Client config: ${out.clientFile}`);
  console.log(`Admin config:  ${out.adminFile} (keep on the server)`);
  console.log("Use BROKER_CONFIG=<file> with CLI/wrappers. No services or existing auth were changed.");
}

async function serve(flags) {
  const port = flags.port === undefined ? 8787 : Number(flags.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("invalid port");
  const server = await require("../server").serve({ dataDir: flags["data-dir"], host: flags.host, port });
  console.log(`Broker listening on ${server.address().address}:${server.address().port}`);
  // Drain in-flight rotations before launchd restarts the process.
  const stop = () => server.close((err) => { if (err) { console.error(err.message); process.exitCode = 1; } });
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
}

async function deploy(flags) {
  const { deploy } = require("../deploy");
  const out = await deploy({
    project: flags.project,
    alertWebhook: flags["alert-webhook"],
    dedicatedProject: flags["dedicated-project"] === true
  });
  console.log(`\n✓ broker deployed: ${out.url}`);
  console.log(`  config saved to ${config.FILE}`);
  console.log(`  next: log in to a provider, then 'broker seed <provider>'`);
}

module.exports = { manage, init, serve, deploy };
