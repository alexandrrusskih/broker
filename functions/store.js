const admin = require("firebase-admin");
const crypto = require("crypto");

const db = admin.firestore();

// Firestore is the source of truth for rotating OAuth tokens (refresh + access).
// Secret Manager is the wrong storage model for a value that changes on every
// refresh. The dedicated project's deny-all rules block every client; only the
// function's admin SDK reaches these documents. The short-lived bootstrap
// credential is the exception and lives in Secret Manager.
const COLLECTION = "oauth_tokens";

// Per-instance cache to avoid a Firestore read on every request. Cleared on
// refresh. Cross-instance correctness is guaranteed by the Firestore lock below.
const cache = new Map();

const docRef = (provider, account = "default") => db.collection(COLLECTION).doc(`${provider}__${account}`);

// Broker config also lives in Firestore and has the same deny-all boundary.
// Short TTL so config changes (e.g. adding gemini creds) propagate without a
// redeploy, and a pre-seed empty read never sticks.
let configCache = null;
let configCachedAt = 0;
const CONFIG_TTL_MS = 60_000;
async function readConfig() {
  if (configCache && Date.now() - configCachedAt < CONFIG_TTL_MS) return configCache;
  const snap = await db.collection("broker_config").doc("_settings").get();
  const data = snap.exists ? snap.data() : null;
  if (data) {
    configCache = data;
    configCachedAt = Date.now();
  }
  return data || {};
}

const key = (provider, account = "default") => `${provider}__${account}`;

async function read(provider, account = "default") {
  const k = key(provider, account);
  if (cache.has(k)) return cache.get(k);
  const snap = await docRef(provider, account).get();
  const data = snap.exists ? snap.data() : null;
  if (data) cache.set(k, data); // never cache a miss — would block the seed transition
  return data;
}

async function write(provider, account, tokenData) {
  const k = key(provider, account);
  const payload = { ...tokenData, updated_at: Date.now() };
  await docRef(provider, account).set(payload, { merge: true });
  cache.set(k, payload);
  return payload;
}

// Names of every account seeded for a provider. `listDocuments` returns refs
// only — no token data is read, so this stays cheap and leaks nothing.
async function listAccounts(provider) {
  const prefix = `${provider}__`;
  // select() reads just this one field — enough to tell a seeded account from a
  // leftover stub, without pulling any token material out of Firestore.
  const snap = await db.collection(COLLECTION).select("refresh_token", "api_key").get();
  return snap.docs
    .filter((d) => d.id.startsWith(prefix))
    .filter((d) => d.get("refresh_token") || d.get("api_key"))
    .map((d) => d.id.slice(prefix.length))
    .sort();
}

// Re-auth alerts are throttled per account: a dead refresh token fails on every
// single request, and a wrapper that probes all accounts would otherwise turn
// one broken seed into a stream of identical Slack messages.
const ALERT_EVERY_MS = 3_600_000;

async function shouldAlert(provider, account, everyMs = ALERT_EVERY_MS) {
  const ref = docRef(provider, account);
  const snap = await ref.get();
  // Never write to an account that is gone: a merge would recreate the document
  // as a stub with only this timestamp, and the stub then fails every refresh
  // forever — resurrecting an account somebody deliberately removed.
  if (!snap.exists) return false;
  const last = snap.data().last_reauth_alert || 0;
  if (Date.now() - last < everyMs) return false;
  await ref.set({ last_reauth_alert: Date.now() }, { merge: true });
  return true;
}

// Forget an account: the document goes, and so does the per-instance cache
// entry — otherwise a warm instance would keep serving the deleted token.
async function remove(provider, account) {
  const ref = docRef(provider, account);
  const snap = await ref.get();
  await ref.delete();
  cache.delete(key(provider, account));
  return snap.exists;
}

function accessIsFresh(tokenData, skewMs = 120_000) {
  if (!tokenData || !tokenData.access_token || !tokenData.expires_at) return false;
  return tokenData.expires_at - Date.now() > skewMs;
}

// Cross-instance distributed lock via a Firestore transaction + a lease field.
// Only the lease holder refreshes; everyone else waits for the just-written token.
// This is what makes the broker the SOLE refresh authority — no refresh_token_reused.
//
// LEASE_MS must comfortably exceed the provider's own refresh timeout (codex.js
// REFRESH_TIMEOUT_MS = 20s) plus margin, so a slow-but-alive OpenAI call can never
// outlive the lease and let a second holder submit the same single-use token.
const LEASE_MS = 60_000;
const WAIT_STEP_MS = 250;

// opts.coalesceFrom: the access_token the caller already holds. When set, the
// acquirer rotates UNLESS the stored access_token has changed since (i.e. someone
// else already refreshed) — used by /oauthRefresh, where codex asks for a genuinely
// new token (proactively at exp-5min or after a 401) and a time-freshness gate would
// hand back the same token and make codex loop. getToken passes nothing and keeps the
// time-freshness gate. Either way concurrent callers coalesce onto one real refresh.
async function withRefreshLease(provider, account, refreshFn, opts = {}) {
  const ref = docRef(provider, account);
  const owner = crypto.randomUUID(); // fencing token: this attempt's identity
  const coalesce = "coalesceFrom" in opts; // presence, not truthiness (a seed can pass undefined)

  // Try to acquire the lease.
  const acquired = await db.runTransaction(async (txn) => {
    const snap = await txn.get(ref);
    const data = snap.exists ? snap.data() : null;

    // Someone already refreshed — use their token. For a coalesce caller that means
    // the stored access changed since it read; otherwise the ordinary freshness gate.
    if (coalesce) {
      if (data && data.access_token && data.access_token !== opts.coalesceFrom) {
        return { holder: false, data };
      }
    } else if (accessIsFresh(data)) {
      return { holder: false, data };
    }

    const now = Date.now();
    const lease = data?.lease_until || 0;
    if (lease > now) return { holder: false, data }; // another instance is refreshing

    // A rotation that began but never cleared: the previous holder may have
    // consumed (and rotated) the token at OpenAI without persisting the result
    // (a crash or timeout between the OpenAI call and the Firestore write). The
    // stored refresh token is now INDETERMINATE — replaying it would trip reuse
    // detection and kill the whole grant. Refuse; the account needs a fresh local
    // login + re-seed. (rotation_pending is only ever cleared by a successful
    // commit below.)
    if (data?.rotation_pending) return { holder: false, pending: true };

    // Fence the lease with an owner id and journal that a rotation is in flight.
    txn.set(
      ref,
      { lease_until: now + LEASE_MS, lease_owner: owner, rotation_pending: true },
      { merge: true }
    );
    return { holder: true, data };
  });

  if (acquired.pending) {
    // "refresh" in the message so getToken's needs-reauth alert path fires.
    throw new Error(
      "codex refresh failed: rotation_pending — a previous refresh did not complete; re-seed required"
    );
  }

  if (!acquired.holder) {
    // Poll until the holder produces the token we are waiting for, or the lease
    // frees. "Produced" differs by mode: a coalesce waiter must see a token that
    // actually ROTATED (access_token !== coalesceFrom) — the pre-rotation token can
    // still look time-fresh, so an accessIsFresh check would return the caller its
    // OWN token and reproduce the refresh loop coalesceFrom exists to kill.
    const resolved = (cur) =>
      coalesce
        ? Boolean(cur && cur.access_token && cur.access_token !== opts.coalesceFrom)
        : accessIsFresh(cur);
    const deadline = Date.now() + LEASE_MS + 2_000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, WAIT_STEP_MS));
      cache.delete(key(provider, account));
      const cur = await read(provider, account);
      if (resolved(cur)) return cur;
      if (!((cur?.lease_until || 0) > Date.now())) break; // holder released w/o success
    }
    cache.delete(key(provider, account));
    const cur = await read(provider, account);
    if (resolved(cur)) return cur;
    throw new Error("codex refresh failed: lease holder did not produce a fresh token");
  }

  let refreshed;
  try {
    refreshed = await refreshFn(acquired.data);
  } catch (err) {
    // Ambiguous outcome: OpenAI may already have consumed and rotated the token.
    // Leave rotation_pending SET (fail-closed — never replay) and only release the
    // lease, so the next request surfaces needs_reauth instead of a silent second
    // submission of a possibly-spent token.
    await ref.set({ lease_until: 0, lease_owner: null }, { merge: true }).catch(() => {});
    throw err;
  }

  // Conditional commit: persist ONLY if we STILL own the lease. A missing, cleared,
  // or different owner means the lease expired mid-flight and another attempt took
  // over; our rotation is indeterminate and must not be written on top of theirs.
  const committed = await db.runTransaction(async (txn) => {
    const snap = await txn.get(ref);
    const cur = snap.exists ? snap.data() : {};
    if (cur.lease_owner !== owner) {
      throw new Error(
        "codex refresh failed: lease lost during rotation — token indeterminate; re-seed required"
      );
    }
    const payload = {
      ...refreshed,
      lease_until: 0,
      lease_owner: null,
      rotation_pending: false,
      updated_at: Date.now()
    };
    txn.set(ref, payload, { merge: true });
    return payload;
  });
  // Cache only AFTER the transaction durably commits — a retried/failed commit must
  // not leave a warm instance serving a token Firestore never stored.
  cache.set(key(provider, account), committed);
  return committed;
}

module.exports = { read, write, accessIsFresh, withRefreshLease, docRef, readConfig, listAccounts, shouldAlert, remove };
