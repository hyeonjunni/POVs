const TERMINAL_ACTIVATION_STATES = new Set(["COMMITTED", "EXPIRED", "FAILED"]);
const BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

async function createActivationKey(getRandomBytes) {
  const bytes = await getRandomBytes();
  if (!(bytes instanceof Uint8Array) || bytes.length !== 32) throw new Error("32바이트 난수가 필요합니다.");
  let encoded = "";
  for (let index = 0; index < bytes.length; index += 3) {
    const value = (bytes[index] << 16) | ((bytes[index + 1] || 0) << 8) | (bytes[index + 2] || 0);
    encoded += BASE64URL[(value >>> 18) & 63];
    encoded += BASE64URL[(value >>> 12) & 63];
    if (index + 1 < bytes.length) encoded += BASE64URL[(value >>> 6) & 63];
    if (index + 2 < bytes.length) encoded += BASE64URL[value & 63];
  }
  return encoded;
}

function isActivationStatus(value) {
  return value && typeof value.operation_id === "string"
    && ["WAITING_CREDENTIAL", "READY", "COMMITTED", "EXPIRED", "FAILED"].includes(value.state);
}

async function captureActivationStatus(request) {
  try {
    return await request();
  } catch (error) {
    if (isActivationStatus(error?.detail)) return error.detail;
    throw error;
  }
}

function activationTimeoutError() {
  return new Error("활성화 상태 확인 시간이 초과되었습니다.");
}

async function withDeadline(request, timeoutMs) {
  if (timeoutMs <= 0) throw activationTimeoutError();
  const controller = new AbortController();
  let timer;
  const boundary = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(activationTimeoutError());
    }, timeoutMs);
  });
  try {
    return await Promise.race([request(controller.signal), boundary]);
  } finally {
    clearTimeout(timer);
  }
}

async function completeActivation({
  idempotencyKey,
  operationId = null,
  assertCurrent = () => {},
  submit,
  getStatus,
  onState = () => {},
  timeoutMs = 60000,
  sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  now = Date.now,
}) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(idempotencyKey)) throw new Error("유효한 activation 키가 필요합니다.");
  const startedAt = now();
  const remaining = () => timeoutMs - (now() - startedAt);
  const baseHeaders = { Prefer: "respond-async", "Idempotency-Key": idempotencyKey };
  const request = async (action) => {
    assertCurrent();
    const result = await captureActivationStatus(() => withDeadline(action, remaining()));
    assertCurrent();
    return result;
  };
  const observe = async (status) => {
    if (!isActivationStatus(status)) throw new Error("활성화 상태를 확인하지 못했습니다.");
    if (operationId && status.operation_id !== operationId) throw new Error("활성화 요청이 일치하지 않습니다.");
    operationId = status.operation_id;
    await onState(status);
    assertCurrent();
  };
  const poll = () => request(signal => getStatus(operationId, { "Idempotency-Key": idempotencyKey }, { signal }));
  const continueRequest = () => request(signal => submit({ ...baseHeaders, "X-Activation-Operation": operationId }, { signal }));
  let response;
  if (operationId) {
    response = await poll();
    await observe(response);
    if (["FAILED", "EXPIRED"].includes(response.state)) return { state: response.state, activation: response };
    // GET may still say WAITING_CREDENTIAL: the original POST performs promotion.
    // COMMITTED is also replayed to retrieve the actual room response.
    response = await continueRequest();
  } else {
    response = await request(signal => submit(baseHeaders, { signal }));
  }

  while (isActivationStatus(response)) {
    await observe(response);
    if (TERMINAL_ACTIVATION_STATES.has(response.state)) {
      return response.state === "COMMITTED"
        ? { state: "COMMITTED", activation: response }
        : { state: response.state, activation: response };
    }
    await withDeadline(
      () => sleep(Math.max(0, Number(response.retry_after_seconds ?? 2) * 1000)),
      remaining()
    );
    const status = await poll();
    await observe(status);
    if (["EXPIRED", "FAILED"].includes(status.state)) {
      return { state: status.state, activation: status };
    }
    response = await continueRequest();
  }

  return { state: "COMMITTED", value: response };
}

function createActivationCoordinator({ createKey, storage, storageKey = "ambient.roomCreateActivation" }) {
  const pending = new Map();
  let generation = 0;
  let storageQueue = Promise.resolve();
  function stored(action) {
    const result = storageQueue.then(action);
    storageQueue = result.catch(() => {});
    return result;
  }

  function abandonedError() {
    const error = new Error("이전 세션의 활성화 요청이 취소되었습니다.");
    error.code = "ACTIVATION_ABANDONED";
    return error;
  }

  function invalidate() {
    generation += 1;
    pending.clear();
  }

  function clear() {
    invalidate();
    return storage ? stored(() => storage.removeItem(storageKey)) : Promise.resolve();
  }

  function changeSession(scope) {
    invalidate();
    return storage ? stored(async () => {
      const raw = await storage.getItem(storageKey);
      if (raw && JSON.parse(raw).scope !== scope) await storage.removeItem(storageKey);
    }) : Promise.resolve();
  }

  async function run({ kind, payload, scope = null, isCurrent = () => true, execute }) {
    const serializedPayload = JSON.stringify(payload);
    if (pending.has(kind) && pending.get(kind).scope !== scope) await clear();
    const runGeneration = generation;
    let entry = pending.get(kind);
    if (!entry) {
      entry = { scope, serializedPayload, attempt: null, context: null };
      pending.set(kind, entry);
    }
    if (kind !== "ROOM_CREATE" && entry.serializedPayload !== serializedPayload) {
      throw new Error("이전 활성화 요청의 결과를 먼저 확인해주세요.");
    }
    const assertCurrent = () => {
      if (generation !== runGeneration || pending.get(kind) !== entry || !isCurrent()) throw abandonedError();
    };
    const durable = storage && kind === "ROOM_CREATE" && scope;
    const save = () => durable ? stored(() => {
      assertCurrent();
      return storage.setItem(storageKey, JSON.stringify(entry.context));
    }) : Promise.resolve();
    if (!entry.attempt) {
      entry.attempt = (async () => {
        assertCurrent();
        if (!entry.context) {
          const raw = durable ? await stored(() => storage.getItem(storageKey)) : null;
          assertCurrent();
          const saved = raw ? JSON.parse(raw) : null;
          if (saved && saved.scope === scope) {
            if (!/^[A-Za-z0-9_-]{43}$/.test(saved.idempotencyKey) || typeof saved.serializedPayload !== "string"
                || (saved.operationId !== null && typeof saved.operationId !== "string")) {
              throw new Error("저장된 가족방 활성화 정보를 확인하지 못했습니다.");
            }
            JSON.parse(saved.serializedPayload);
            entry.context = saved;
          } else {
            entry.context = { scope, serializedPayload, idempotencyKey: await createKey(), operationId: null };
          }
        }
        assertCurrent();
        await save(); // Never start an activation before its capability/body are durable.
        assertCurrent();
        const result = await execute(entry.context.idempotencyKey, {
          operationId: entry.context.operationId,
          payload: JSON.parse(entry.context.serializedPayload),
          assertCurrent,
          onState: async status => {
            assertCurrent();
            entry.context.operationId = status.operation_id;
            await save();
            assertCurrent();
          },
        });
        assertCurrent();
        if (TERMINAL_ACTIVATION_STATES.has(result?.state)) {
          if (durable) await stored(() => { assertCurrent(); return storage.removeItem(storageKey); });
          assertCurrent();
        }
        return result;
      })();
    }
    const attempt = entry.attempt;
    try {
      const result = await attempt;
      if (["COMMITTED", "EXPIRED", "FAILED"].includes(result?.state) && pending.get(kind) === entry) pending.delete(kind);
      return result;
    } catch (error) {
      if (entry.attempt === attempt) entry.attempt = null;
      // Transport/HTTP errors are not evidence of a terminal activation state.
      // Preserve signup's existing correction flow for definitive HTTP errors.
      if (kind !== "ROOM_CREATE" && Number.isInteger(error?.status) && error.status < 500
          && pending.get(kind) === entry) pending.delete(kind);
      throw error;
    }
  }

  return { clear, changeSession, invalidate, run };
}

module.exports = { completeActivation, createActivationCoordinator, createActivationKey, isActivationStatus, TERMINAL_ACTIVATION_STATES };
