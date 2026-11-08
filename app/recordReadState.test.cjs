const test = require("node:test");
const assert = require("node:assert/strict");
const { createReadScopeKey, createRecordReadState } = require("./src/features/records/readState.cjs");

const scope = (overrides = {}) => ({ baseUrl: "http://localhost:8001/api/v1", userId: "user-1", roomId: "room-1", ...overrides });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
function memoryStorage(initial = []) {
  const data = new Map(initial);
  return { data, async getItem(key) { return data.get(key) ?? null; }, async setItem(key, value) { data.set(key, value); } };
}

test("missing storage starts unread; viewing a list does not mark read; opening persists only IDs", async () => {
  const storage = memoryStorage();
  const state = createRecordReadState({ storage });
  const key = createReadScopeKey(scope());
  const loading = state.setScope(scope());
  assert.equal(state.getStateFor(key).ready, false);
  assert.equal(state.isUnread("record-1"), false, "no New flash during hydration");
  await loading;
  assert.equal(state.isUnread("record-1"), true);
  assert.equal(state.isUnread("record-1"), true);
  assert.equal(storage.data.size, 0);
  await state.markRead("record-1");
  assert.equal(state.isUnread("record-1"), false);
  assert.equal(state.isUnread("record-2"), true);
  assert.deepEqual(JSON.parse(storage.data.get(key)), { version: 1, recordIds: ["record-1"] });
});

test("read state survives a new controller, logout and relogin", async () => {
  const storage = memoryStorage();
  const first = createRecordReadState({ storage });
  await first.setScope(scope());
  await first.markRead("record-1");
  first.disconnect();
  const second = createRecordReadState({ storage });
  await second.setScope(scope());
  assert.equal(second.isUnread("record-1"), false);
  await second.setScope({});
  assert.equal(second.isUnread("record-1"), false);
  assert.equal(await second.markRead("record-2"), false);
  await second.setScope(scope());
  assert.equal(second.isUnread("record-1"), false);
  assert.equal(second.isUnread("record-2"), true);
});

test("identical record IDs remain isolated by exact API, account and room", async () => {
  const state = createRecordReadState({ storage: memoryStorage() });
  await state.setScope(scope());
  await state.markRead("shared-record-id");
  for (const other of [scope({ userId: "user-2" }), scope({ roomId: "room-2" }), scope({ baseUrl: "http://localhost:8002/api/v1" })]) {
    await state.setScope(other);
    assert.equal(state.isUnread("shared-record-id"), true);
  }
  await state.setScope(scope());
  assert.equal(state.isUnread("shared-record-id"), false);
  assert.notEqual(createReadScopeKey(scope({ userId: "x,y", roomId: "z" })), createReadScopeKey(scope({ userId: "x", roomId: "y,z" })));
});

test("concurrent opens merge persisted history without dropping records", async () => {
  const storage = memoryStorage([[createReadScopeKey(scope()), JSON.stringify({ version: 1, recordIds: ["older"] })]]);
  const state = createRecordReadState({ storage });
  await state.setScope(scope());
  await Promise.all([state.markRead("record-1"), state.markRead("record-2"), state.markRead("record-1")]);
  assert.deepEqual(new Set(JSON.parse(storage.data.get(createReadScopeKey(scope()))).recordIds), new Set(["older", "record-1", "record-2"]));
});

test("opening while hydration is pending preserves older persisted reads", async () => {
  const gate = deferred();
  const storage = memoryStorage([[createReadScopeKey(scope()), JSON.stringify({ version: 1, recordIds: ["older"] })]]);
  const getItem = storage.getItem;
  storage.getItem = async key => { await gate.promise; return getItem(key); };
  const state = createRecordReadState({ storage });
  const loading = state.setScope(scope());
  const opening = state.markRead("just-opened");
  gate.resolve();
  await Promise.all([loading, opening]);
  assert.equal(state.isUnread("older"), false);
  assert.equal(state.isUnread("just-opened"), false);
});

test("delayed hydration and stale open callbacks cannot affect another room", async () => {
  const gate = deferred();
  const originalScope = scope();
  const originalKey = createReadScopeKey(originalScope);
  const nextScope = scope({ roomId: "room-2" });
  const nextKey = createReadScopeKey(nextScope);
  let currentKey = originalKey;
  const storage = memoryStorage([[originalKey, JSON.stringify({ version: 1, recordIds: ["record-1"] })]]);
  const getItem = storage.getItem;
  storage.getItem = async key => { if (key === originalKey) await gate.promise; return getItem(key); };
  const state = createRecordReadState({ storage, readCurrentScopeKey: () => currentKey });
  const originalLoad = state.setScope(originalScope);
  currentKey = nextKey;
  assert.equal(state.getStateFor(originalKey).ready, false, "old state hidden before layout effect");
  assert.equal(await state.markRead("record-2", originalKey), false);
  await state.setScope(nextScope);
  assert.equal(await state.markRead("record-2", originalKey), false);
  gate.resolve();
  await originalLoad;
  assert.equal(state.isUnread("record-1", nextKey), true);
  assert.equal(state.isUnread("record-2", nextKey), true);
  assert.equal(storage.data.has(nextKey), false);
});

test("an in-flight save finishes only for the captured room after a scope switch", async () => {
  const gate = deferred();
  const started = deferred();
  const storage = memoryStorage();
  const setItem = storage.setItem;
  storage.setItem = async (key, value) => { started.resolve(); await gate.promise; return setItem(key, value); };
  const state = createRecordReadState({ storage });
  await state.setScope(scope());
  const saving = state.markRead("record-1");
  await started.promise;
  await state.setScope(scope({ roomId: "room-2" }));
  gate.resolve();
  await saving;
  assert.equal(state.isUnread("record-1"), true);
  assert.equal(storage.data.has(createReadScopeKey(scope({ roomId: "room-2" }))), false);
  await state.setScope(scope());
  assert.equal(state.isUnread("record-1"), false);
});

test("remounts merge rather than overwrite one another's pending reads", async () => {
  const storage = memoryStorage();
  const first = createRecordReadState({ storage });
  const second = createRecordReadState({ storage });
  await Promise.all([first.setScope(scope()), second.setScope(scope())]);
  await Promise.all([first.markRead("record-1"), second.markRead("record-2")]);
  const restored = createRecordReadState({ storage });
  await restored.setScope(scope());
  assert.equal(restored.isUnread("record-1"), false);
  assert.equal(restored.isUnread("record-2"), false);
});

test("corrupt storage recovers after an explicit open and reports a load error", async () => {
  for (const invalid of ["broken JSON", JSON.stringify({ version: 999, recordIds: [] }), JSON.stringify({ version: 1, recordIds: [null] })]) {
    const storage = memoryStorage([[createReadScopeKey(scope()), invalid]]);
    const state = createRecordReadState({ storage });
    await state.setScope(scope());
    assert.equal(state.getStateFor(createReadScopeKey(scope())).ready, true);
    assert.ok(state.getStateFor(createReadScopeKey(scope())).error);
    assert.equal(state.isUnread("record-1"), true);
    await state.markRead("record-1");
    assert.equal(state.getStateFor(createReadScopeKey(scope())).error, null);
    assert.equal(state.isUnread("record-1"), false);
  }
});

test("storage failures retain session reads, expose an error and allow retry", async () => {
  const storage = memoryStorage();
  const setItem = storage.setItem;
  storage.setItem = async () => { throw new Error("storage unavailable"); };
  const state = createRecordReadState({ storage });
  await state.setScope(scope());
  await state.markRead("record-1");
  assert.equal(state.isUnread("record-1"), false);
  assert.ok(state.getStateFor(createReadScopeKey(scope())).error);
  storage.setItem = setItem;
  await state.markRead("record-2");
  assert.equal(state.getStateFor(createReadScopeKey(scope())).error, null);
  assert.deepEqual(new Set(JSON.parse(storage.data.get(createReadScopeKey(scope()))).recordIds), new Set(["record-1", "record-2"]));
});

test("disconnect prevents marks and notifications; reconnect restores behavior", async () => {
  let notifications = 0;
  const state = createRecordReadState({ storage: memoryStorage(), onChange: () => { notifications++; } });
  await state.setScope(scope());
  state.disconnect();
  const before = notifications;
  assert.equal(await state.markRead("record-1"), false);
  assert.equal(notifications, before);
  state.connect();
  await state.markRead("record-1");
  assert.equal(state.isUnread("record-1"), false);
});
