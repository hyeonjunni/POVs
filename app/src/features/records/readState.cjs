const STORAGE_PREFIX = "@ambient/record-read/v1:";
const LOAD_ERROR = "이 기기의 읽음 표시를 불러오지 못했습니다.";
const SAVE_ERROR = "읽음 표시를 저장하지 못했습니다. 현재 화면에는 반영됩니다.";
const storageQueues = new WeakMap();

function createReadScopeKey({ baseUrl, userId, roomId } = {}) {
  const values = [baseUrl, userId, roomId];
  if (values.some(value => typeof value !== "string" || !value.trim())) return null;
  // Exact values and a tuple avoid collisions between servers, accounts and rooms.
  return STORAGE_PREFIX + JSON.stringify(values);
}

function decodeReadIds(value) {
  if (value == null) return new Set();
  const parsed = JSON.parse(value);
  if (parsed?.version !== 1 || !Array.isArray(parsed.recordIds)
      || parsed.recordIds.some(id => typeof id !== "string" || !id.trim())) {
    throw new SyntaxError("Invalid record read state");
  }
  return new Set(parsed.recordIds);
}

// Also order writes across hook remounts. Every write merges the persisted IDs,
// so an older instance cannot erase a newer instance's completed reads.
function enqueueStorage(storage, key, operation) {
  let queues = storageQueues.get(storage);
  if (!queues) { queues = new Map(); storageQueues.set(storage, queues); }
  const previous = queues.get(key) || Promise.resolve();
  const next = previous.catch(() => {}).then(operation);
  queues.set(key, next);
  const cleanup = () => { if (queues.get(key) === next) queues.delete(key); };
  next.then(cleanup, cleanup);
  return next;
}

function createRecordReadState({ storage, onChange = () => {}, readCurrentScopeKey } = {}) {
  const entries = new Map();
  let activeKey = null;
  let connected = true;
  const isCurrent = key => Boolean(key && key === activeKey
    && (!readCurrentScopeKey || readCurrentScopeKey() === key));
  const notify = key => { if (connected && isCurrent(key)) onChange(); };

  function setScope(scope) {
    const key = createReadScopeKey(scope);
    activeKey = key;
    if (!key) return Promise.resolve();
    let entry = entries.get(key);
    if (!entry) {
      entry = { ids: new Set(), ready: false, error: null, loading: null };
      entries.set(key, entry);
      entry.loading = enqueueStorage(storage, key, async () => {
        try {
          const ids = decodeReadIds(await storage.getItem(key));
          ids.forEach(id => entry.ids.add(id));
        } catch (_error) {
          entry.error = LOAD_ERROR;
        }
        entry.ready = true;
        notify(key);
      });
    }
    notify(key);
    return entry.loading;
  }

  function getStateFor(key) {
    const entry = isCurrent(key) ? entries.get(key) : null;
    return { ready: Boolean(entry?.ready), error: entry?.error || null, scopeKey: key };
  }

  function isUnread(recordId, expectedKey = activeKey) {
    const entry = isCurrent(expectedKey) ? entries.get(expectedKey) : null;
    return Boolean(entry?.ready && typeof recordId === "string" && recordId.trim()
      && !entry.ids.has(recordId));
  }

  async function markRead(recordId, expectedKey = activeKey) {
    if (!connected || !isCurrent(expectedKey) || typeof recordId !== "string" || !recordId.trim()) return false;
    const entry = entries.get(expectedKey);
    if (!entry) return false;
    entry.ids.add(recordId);
    notify(expectedKey);
    // The key is captured when the record is opened, never read after awaiting.
    await enqueueStorage(storage, expectedKey, async () => {
      try {
        let persisted;
        try { persisted = decodeReadIds(await storage.getItem(expectedKey)); }
        catch (error) {
          if (!(error instanceof SyntaxError)) throw error;
          persisted = new Set();
        }
        persisted.forEach(id => entry.ids.add(id));
        await storage.setItem(expectedKey, JSON.stringify({ version: 1, recordIds: [...entry.ids] }));
        entry.error = null;
      } catch (_error) {
        entry.error = SAVE_ERROR;
      }
      notify(expectedKey);
    });
    return true;
  }

  return {
    setScope, getStateFor, isUnread, markRead,
    connect() { connected = true; notify(activeKey); },
    disconnect() { connected = false; },
  };
}

module.exports = { createReadScopeKey, createRecordReadState };
