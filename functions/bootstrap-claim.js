"use strict";

async function claimBootstrap(db, ref, payload) {
  return db.runTransaction(async (txn) => {
    const snap = await txn.get(ref);
    if (snap.exists && snap.data()?.broker_key) return false;
    txn.set(ref, payload, { merge: true });
    return true;
  });
}

module.exports = { claimBootstrap };
