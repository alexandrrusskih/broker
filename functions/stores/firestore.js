// Firestore keeps the existing document layout and cross-instance transaction
// boundary. No Firebase dependency is loaded by the self-hosted server.
function createFirestoreAdapter(db) {
  const collection = db.collection("oauth_tokens");
  const ref = (provider, account) => collection.doc(`${provider}__${account}`);
  const settings = db.collection("broker_config").doc("_settings");
  const readRef = async (target) => {
    const snap = await target.get();
    return snap.exists ? snap.data() : null;
  };
  const transactRef = (target, update) => db.runTransaction(async (txn) => {
    const snap = await txn.get(target);
    const change = update(snap.exists ? snap.data() : null);
    if (change.data === null) txn.delete(target);
    else if (change.data !== undefined) txn.set(target, change.data);
    return change.result;
  });
  return {
    read: (provider, account) => readRef(ref(provider, account)),
    transact: (provider, account, update) => transactRef(ref(provider, account), update),
    readConfig: async () => (await readRef(settings)) || {},
    transactConfig: (update) => transactRef(settings, update),
    async listAccounts(provider) {
      const prefix = `${provider}__`;
      const snap = await collection.select("refresh_token", "api_key").get();
      return snap.docs.filter((d) => d.id.startsWith(prefix) && (d.get("refresh_token") || d.get("api_key")))
        .map((d) => d.id.slice(prefix.length)).sort();
    }
  };
}

module.exports = { createFirestoreAdapter };
