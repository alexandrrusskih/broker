const crypto = require("crypto");

const LEASE_MS = 60_000;
const WAIT_STEP_MS = 250;
const pendingError = () => new Error("refresh failed: rotation_pending — a previous refresh did not complete; re-seed required");

function accessIsFresh(data, skewMs = 120_000) {
  return Boolean(data?.access_token && data.expires_at - Date.now() > skewMs);
}

// The adapter supplies atomic read/modify/write, not OAuth logic. Firestore uses
// transactions; files use a per-account queue in the single broker process.
// No token cache: another instance, a re-seed or deletion must be visible now.
function createStore(adapter) {
  const read = (provider, account = "default") => adapter.read(provider, account);

  async function write(provider, account, fields) {
    return adapter.transact(provider, account, (current) => {
      const data = { ...current, ...fields, updated_at: Date.now() };
      return { data, result: data };
    });
  }

  async function remove(provider, account) {
    return adapter.transact(provider, account, (current) => ({ data: null, result: Boolean(current) }));
  }

  async function shouldAlert(provider, account, everyMs = 3_600_000) {
    return adapter.transact(provider, account, (current) => {
      if (!current || Date.now() - (current.last_reauth_alert || 0) < everyMs) return { result: false };
      return { data: { ...current, last_reauth_alert: Date.now() }, result: true };
    });
  }

  // coalesceFrom is used by Codex's own refresh callback: it needs a NEW access
  // token even if the current one still looks fresh. Concurrent calls share it.
  async function withRefreshLease(provider, account, refreshFn, opts = {}) {
    const owner = crypto.randomUUID();
    const coalesce = Object.hasOwn(opts, "coalesceFrom");
    const resolved = (data) => coalesce
      ? Boolean(data?.access_token && data.access_token !== opts.coalesceFrom)
      : accessIsFresh(data);

    const acquired = await adapter.transact(provider, account, (data) => {
      if (!data || !(data.refresh_token || data.api_key)) throw new Error("account not seeded; re-seed required");
      if (resolved(data)) return { result: { ready: data } };
      if (data.lease_until > Date.now()) return { result: { waiting: true } };
      // Persist BEFORE submitting a single-use refresh token. A crashed or timed
      // out rotation is indeterminate, even after the lease expires: never replay.
      if (data.rotation_pending) throw pendingError();
      return {
        data: { ...data, lease_until: Date.now() + LEASE_MS, lease_owner: owner, rotation_pending: true },
        result: { holder: true, previous: data }
      };
    });
    if (acquired.ready) return acquired.ready;

    if (!acquired.holder) {
      const deadline = Date.now() + LEASE_MS + 2_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, WAIT_STEP_MS));
        const current = await read(provider, account);
        if (resolved(current)) return current;
        if (!(current?.lease_until > Date.now())) {
          if (current?.rotation_pending) throw pendingError();
          break;
        }
      }
      throw new Error("refresh failed: lease holder did not produce a fresh token");
    }

    let refreshed;
    try {
      refreshed = await refreshFn(acquired.previous);
    } catch (err) {
      // Fence failure cleanup as well as success: a concurrent seed/delete wins.
      await adapter.transact(provider, account, (current) => current?.lease_owner === owner
        ? { data: { ...current, lease_until: 0, lease_owner: null } }
        : {}).catch(() => {});
      throw err;
    }

    return adapter.transact(provider, account, (current) => {
      if (current?.lease_owner !== owner) {
        throw new Error("refresh failed: lease lost during rotation — token indeterminate; re-seed required");
      }
      const data = {
        ...current, ...refreshed, lease_until: 0, lease_owner: null,
        rotation_pending: false, updated_at: Date.now()
      };
      return { data, result: data };
    });
  }

  return {
    read, write, remove, shouldAlert, accessIsFresh, withRefreshLease,
    readConfig: () => adapter.readConfig(),
    listAccounts: (provider) => adapter.listAccounts(provider),
    writeConfig: (fields) => adapter.transactConfig((current) => ({ data: { ...current, ...fields } })),
    claimConfig: (fields) => adapter.transactConfig((current) => current?.broker_key
      ? { result: false } : { data: { ...current, ...fields }, result: true })
  };
}

module.exports = { createStore, accessIsFresh };
