const config = require("./config");

async function get(provider, format = "raw", account = "default") {
  const cfg = config.require();
  const r = await fetch(`${cfg.url}/getToken?provider=${provider}&account=${encodeURIComponent(account)}&format=${format}`, {
    headers: { "x-broker-key": cfg.key }
  });
  const body = await r.text();
  if (!r.ok) throw new Error(`getToken ${r.status}: ${body.slice(0, 200)}`);
  return body;
}

module.exports = { get };
