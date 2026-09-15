const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");

async function readJson(file) {
  try { return JSON.parse(await fs.readFile(file, "utf8")); }
  catch (err) { if (err.code === "ENOENT") return null; throw err; }
}

async function syncDirectory(dir) {
  const handle = await fs.open(dir, "r");
  try { await handle.sync(); } finally { await handle.close(); }
}

async function writeJson(file, data) {
  const dir = path.dirname(file);
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const temp = path.join(dir, `.write-${crypto.randomUUID()}`);
  try {
    const handle = await fs.open(temp, "wx", 0o600);
    try {
      await handle.writeFile(JSON.stringify(data, null, 2) + "\n");
      await handle.sync();
    } finally { await handle.close(); }
    await fs.rename(temp, file);
    await syncDirectory(dir);
  } finally {
    await fs.unlink(temp).catch((err) => { if (err.code !== "ENOENT") throw err; });
  }
}

// One broker process owns a data directory. Do not share it via NFS or run a
// worker pool: remote clients use HTTP, never these files. Queues cover only the
// short read/modify/rename operation, not the network refresh or other accounts.
function createFileAdapter(dataDir) {
  const root = path.resolve(dataDir);
  const queues = new Map();
  // Prefix the encoded component so even "." and ".." remain ordinary names.
  const component = (value) => `_${encodeURIComponent(String(value))}`;
  const accountDir = (provider) => path.join(root, "accounts", component(provider));
  const accountFile = (provider, account) => path.join(accountDir(provider), `${component(account)}.json`);
  const configFile = path.join(root, "settings.json");
  function transactFile(file, update) {
    const previous = queues.get(file) || Promise.resolve();
    const operation = previous.catch(() => {}).then(async () => {
      const change = update(await readJson(file));
      if (change.data === null) {
        try { await fs.unlink(file); await syncDirectory(path.dirname(file)); }
        catch (err) { if (err.code !== "ENOENT") throw err; }
      } else if (change.data !== undefined) await writeJson(file, change.data);
      return change.result;
    });
    queues.set(file, operation);
    const cleanup = () => { if (queues.get(file) === operation) queues.delete(file); };
    operation.then(cleanup, cleanup);
    return operation;
  }
  return {
    read: (provider, account) => readJson(accountFile(provider, account)),
    transact: (provider, account, update) => transactFile(accountFile(provider, account), update),
    readConfig: async () => (await readJson(configFile)) || {},
    transactConfig: (update) => transactFile(configFile, update),
    async listAccounts(provider) {
      let files;
      try { files = await fs.readdir(accountDir(provider)); }
      catch (err) { if (err.code === "ENOENT") return []; throw err; }
      const accounts = [];
      for (const file of files.filter((name) => name.startsWith("_") && name.endsWith(".json"))) {
        const token = await readJson(path.join(accountDir(provider), file));
        if (token?.refresh_token || token?.api_key) accounts.push(decodeURIComponent(file.slice(1, -5)));
      }
      return accounts.sort();
    }
  };
}

module.exports = { createFileAdapter, readJson, writeJson };
