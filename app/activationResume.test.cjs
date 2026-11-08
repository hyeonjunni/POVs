const { test } = require('node:test');
const assert = require('node:assert/strict');
const { completeActivation, createActivationCoordinator } = require('./activation.cjs');

function storageFixture() {
  const values = new Map();
  return { values, async getItem(k) { return values.get(k) || null; },
    async setItem(k, v) { values.set(k, v); }, async removeItem(k) { values.delete(k); } };
}
const waiting = { operation_id: 'room-operation', state: 'WAITING_CREDENTIAL', retry_after_seconds: 0 };

for (const reload of [false, true]) test(`timeout retry preserves operation, capability and original body (reload=${reload})`, async () => {
  const storage = storageFixture();
  let allocations = 0;
  const make = () => createActivationCoordinator({ storage, createKey: async () => String(++allocations).repeat(43) });
  let coordinator = make();
  const calls = [];
  let timedOut = false;
  const execute = (key, context = {}) => completeActivation({
    idempotencyKey: key, ...context,
    submit: async headers => { calls.push(['post', headers, context.payload]); return timedOut ? { room_id: 'room-1' } : waiting; },
    getStatus: async id => {
      calls.push(['get', id]);
      if (!timedOut) { timedOut = true; throw new Error('poll timeout'); }
      return { ...waiting, state: 'READY' };
    }, sleep: async () => {},
  });
  await assert.rejects(coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1@server', payload: { name: 'Original <family>' }, execute }), /poll timeout/);
  assert.equal(storage.values.size, 1);
  if (reload) coordinator = make();
  const result = await coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1@server', payload: { name: 'Edited' }, execute });
  assert.equal(result.value.room_id, 'room-1');
  assert.equal(allocations, 1);
  assert.deepEqual(calls.map(call => call.slice(0, 2)), [
    ['post', { Prefer: 'respond-async', 'Idempotency-Key': '1'.repeat(43) }],
    ['get', 'room-operation'], ['get', 'room-operation'],
    ['post', { Prefer: 'respond-async', 'Idempotency-Key': '1'.repeat(43), 'X-Activation-Operation': 'room-operation' }],
  ]);
  assert.deepEqual(calls.filter(c => c[0] === 'post').map(c => c[2]), [{ name: 'Original <family>' }, { name: 'Original <family>' }]);
  assert.equal(storage.values.size, 0);
});

test('logout during polling rejects late state and prevents continuation POST', async () => {
  const storage = storageFixture();
  const coordinator = createActivationCoordinator({ storage, createKey: async () => 'a'.repeat(43) });
  let release, started;
  const gate = new Promise(resolve => { release = resolve; });
  const polling = new Promise(resolve => { started = resolve; });
  let posts = 0;
  const pending = coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' },
    execute: (key, context = {}) => completeActivation({ idempotencyKey: key, ...context,
      submit: async () => { posts++; return waiting; },
      getStatus: async () => { started(); return gate; }, sleep: async () => {} }) });
  await polling;
  await coordinator.clear();
  release({ ...waiting, state: 'READY' });
  await assert.rejects(pending, { code: 'ACTIVATION_ABANDONED' });
  assert.equal(posts, 1);
  assert.equal(storage.values.size, 0);
});

test('nonterminal HTTP error retains persisted activation; terminal states clear it', async () => {
  for (const terminal of ['FAILED', 'EXPIRED']) {
    const storage = storageFixture();
    const coordinator = createActivationCoordinator({ storage, createKey: async () => 'b'.repeat(43) });
    await assert.rejects(coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' },
      execute: async () => { throw Object.assign(new Error('temporary conflict'), { status: 409 }); } }), /temporary conflict/);
    assert.equal(storage.values.size, 1);
    await coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' }, execute: async () => ({ state: terminal }) });
    assert.equal(storage.values.size, 0);
  }
});

test('real polling deadline preserves the capability and operation across restart', async () => {
  const storage = storageFixture();
  const createKey = async () => 'd'.repeat(43);
  const coordinator = createActivationCoordinator({ storage, createKey });
  await assert.rejects(coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' },
    execute: (key, context) => completeActivation({ idempotencyKey: key, ...context,
      submit: async () => waiting, getStatus: () => new Promise(() => {}), sleep: async () => {}, timeoutMs: 15 }) }), /시간이 초과/);
  const restored = createActivationCoordinator({ storage, createKey: async () => { throw new Error('must not allocate'); } });
  await restored.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' }, execute: async (key, context) => {
    assert.equal(key, 'd'.repeat(43));
    assert.equal(context.operationId, 'room-operation');
    return { state: 'FAILED' };
  } });
});

test('changed account or server never replays stored activation', async () => {
  for (const scope of ['user-2@server', 'user-1@other-server']) {
    const storage = storageFixture();
    let allocations = 0;
    const createKey = async () => String(++allocations).repeat(43);
    const first = createActivationCoordinator({ storage, createKey });
    await assert.rejects(first.run({ kind: 'ROOM_CREATE', scope: 'user-1@server', payload: { name: 'private old room' },
      execute: async (_key, context) => { await context.onState(waiting); throw new Error('timeout'); } }), /timeout/);
    const second = createActivationCoordinator({ storage, createKey });
    await second.run({ kind: 'ROOM_CREATE', scope, payload: { name: 'new room' }, execute: async (key, context) => {
      assert.equal(key, '2'.repeat(43));
      assert.equal(context.operationId, null);
      assert.deepEqual(context.payload, { name: 'new room' });
      return { state: 'COMMITTED', value: {} };
    } });
    assert.equal(storage.values.size, 0);
  }
});

test('storage failure prevents initial POST, and retry retains the generated key', async () => {
  const storage = storageFixture();
  const save = storage.setItem;
  storage.setItem = async () => { throw new Error('disk failure'); };
  let allocations = 0, calls = 0;
  const coordinator = createActivationCoordinator({ storage, createKey: async () => { allocations++; return 's'.repeat(43); } });
  const request = { kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' },
    execute: async () => { calls++; return { state: 'COMMITTED', value: {} }; } };
  await assert.rejects(coordinator.run(request), /disk failure/);
  assert.equal(calls, 0);
  storage.setItem = save;
  await coordinator.run(request);
  assert.equal(calls, 1);
  assert.equal(allocations, 1);
});

test('WAITING_CREDENTIAL and COMMITTED poll results both use explicit original POST per backend contract', async () => {
  for (const state of ['WAITING_CREDENTIAL', 'COMMITTED']) {
    const result = await completeActivation({ idempotencyKey: 'w'.repeat(43), operationId: 'room-operation',
      getStatus: async () => ({ ...waiting, state }),
      submit: async headers => {
        assert.equal(headers['X-Activation-Operation'], 'room-operation');
        assert.equal(headers['Idempotency-Key'], 'w'.repeat(43));
        return { room_id: 'room-1' };
      } });
    assert.deepEqual(result, { state: 'COMMITTED', value: { room_id: 'room-1' } });
  }
});

test('unmount invalidates pending responses without deleting restart context', async () => {
  const storage = storageFixture();
  const coordinator = createActivationCoordinator({ storage, createKey: async () => 'u'.repeat(43) });
  let release, start;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { start = resolve; });
  const pending = coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' },
    execute: async (_key, context) => { await context.onState(waiting); start(); return gate; } });
  await started;
  coordinator.invalidate();
  release({ state: 'COMMITTED', value: {} });
  await assert.rejects(pending, { code: 'ACTIVATION_ABANDONED' });
  assert.equal(storage.values.size, 1);
});

test('signup conflict still allows corrected credentials without persisting passwords', async () => {
  const storage = storageFixture();
  const coordinator = createActivationCoordinator({ storage, createKey: async () => 's'.repeat(43) });
  await assert.rejects(coordinator.run({ kind: 'SIGNUP', payload: { username: 'taken', password: 'secret' },
    execute: async () => { throw Object.assign(new Error('identity_conflict'), { status: 409 }); } }), /identity_conflict/);
  const result = await coordinator.run({ kind: 'SIGNUP', payload: { username: 'corrected', password: 'secret' },
    execute: async () => ({ state: 'COMMITTED', value: {} }) });
  assert.equal(result.state, 'COMMITTED');
  assert.equal(storage.values.size, 0);
});

test('same-account login preserves room context, account change discards it', async () => {
  const storage = storageFixture();
  const coordinator = createActivationCoordinator({ storage, createKey: async () => 'l'.repeat(43) });
  await assert.rejects(coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' },
    execute: async (_key, context) => { await context.onState(waiting); throw new Error('timeout'); } }), /timeout/);
  assert.equal(typeof coordinator.changeSession, 'function');
  await coordinator.changeSession('user-1');
  assert.equal(storage.values.size, 1);
  await assert.rejects(coordinator.run({ kind: 'ROOM_CREATE', scope: 'user-1', payload: { name: 'room' },
    execute: async (_key, context) => { assert.equal(context.operationId, 'room-operation'); throw new Error('timeout'); } }), /timeout/);
  await coordinator.changeSession('user-2');
  assert.equal(storage.values.size, 0);
});
