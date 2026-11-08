const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { completeActivation, createActivationCoordinator, createActivationKey } = require("./activation.cjs");

test("activation key encodes exactly 32 random bytes as canonical base64url", async () => {
  const key = await createActivationKey(async () => Uint8Array.from({ length: 32 }, (_, index) => index));
  assert.match(key, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(key, "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8");
});

test("activation observes state and resumes the original request until committed", async () => {
  const calls = [];
  const responses = [
    { operation_id: "operation-1", kind: "SIGNUP", state: "WAITING_CREDENTIAL", expires_at: 10, retry_after_seconds: 2 },
    { access_token: "token", user: { user_id: "user-1" } },
  ];
  const result = await completeActivation({
    idempotencyKey: "a".repeat(43),
    submit: async (headers) => { calls.push(["post", headers]); return responses.shift(); },
    getStatus: async (operationId, headers) => {
      calls.push(["get", operationId, headers]);
      return { operation_id: operationId, kind: "SIGNUP", state: "WAITING_CREDENTIAL", expires_at: 10, retry_after_seconds: 2 };
    },
    sleep: async () => {},
  });

  assert.deepEqual(result, { state: "COMMITTED", value: { access_token: "token", user: { user_id: "user-1" } } });
  assert.deepEqual(calls, [
    ["post", { Prefer: "respond-async", "Idempotency-Key": "a".repeat(43) }],
    ["get", "operation-1", { "Idempotency-Key": "a".repeat(43) }],
    ["post", { Prefer: "respond-async", "Idempotency-Key": "a".repeat(43), "X-Activation-Operation": "operation-1" }],
  ]);
});

test("activation returns failed or expired states without treating them as committed", async () => {
  for (const state of ["FAILED", "EXPIRED"]) {
    const status = { operation_id: `operation-${state}`, kind: "ROOM_CREATE", state, expires_at: 10, retry_after_seconds: null };
    const result = await completeActivation({
      idempotencyKey: "b".repeat(43),
      submit: async () => status,
      getStatus: async () => { throw new Error("should not poll terminal state"); },
    });
    assert.deepEqual(result, { state, activation: status });
  }
});

test("activation stops after the configured timeout", async () => {
  const pending = { operation_id: "operation-1", kind: "SIGNUP", state: "WAITING_CREDENTIAL", expires_at: 10, retry_after_seconds: 2 };
  await assert.rejects(completeActivation({
    idempotencyKey: "c".repeat(43),
    submit: async () => pending,
    getStatus: async () => pending,
    timeoutMs: 1,
    sleep: async () => {},
    now: (() => { let value = 0; return () => value += 2; })(),
  }), /시간이 초과/);
});

test("activation aborts a hung initial submit at the deadline", async () => {
  let requestSignal;
  await assert.rejects(completeActivation({
    idempotencyKey: "d".repeat(43),
    submit: async (_headers, options) => {
      requestSignal = options.signal;
      return new Promise(() => {});
    },
    getStatus: async () => { throw new Error("should not poll"); },
    timeoutMs: 5,
  }), /시간이 초과/);
  assert.equal(requestSignal.aborted, true);
});

test("activation aborts a hung status request at the deadline", async () => {
  let requestSignal;
  const pending = { operation_id: "operation-1", kind: "SIGNUP", state: "WAITING_CREDENTIAL", expires_at: 10, retry_after_seconds: 0 };
  await assert.rejects(completeActivation({
    idempotencyKey: "e".repeat(43),
    submit: async () => pending,
    getStatus: async (_operationId, _headers, options) => {
      requestSignal = options.signal;
      return new Promise(() => {});
    },
    timeoutMs: 5,
    sleep: async () => {},
  }), /시간이 초과/);
  assert.equal(requestSignal.aborted, true);
});

test("room retry after a lost committed response reuses one identity and creates one room", async () => {
  const keys = [];
  const roomsByKey = new Map();
  let loseResponse = true;
  const coordinator = createActivationCoordinator({ createKey: async () => "r".repeat(43) });
  const payload = { name: "Family" };
  const execute = async (key) => {
    keys.push(key);
    if (!roomsByKey.has(key)) roomsByKey.set(key, { room_id: `room-${roomsByKey.size + 1}` });
    if (loseResponse) {
      loseResponse = false;
      throw new Error("response lost");
    }
    return { state: "COMMITTED", value: roomsByKey.get(key) };
  };

  await assert.rejects(coordinator.run({ kind: "ROOM_CREATE", payload, execute }), /response lost/);
  const result = await coordinator.run({ kind: "ROOM_CREATE", payload: { name: "Family" }, execute });

  assert.deepEqual(result.value, { room_id: "room-1" });
  assert.deepEqual(keys, ["r".repeat(43), "r".repeat(43)]);
  assert.equal(roomsByKey.size, 1);
});

test("signup retry keeps the same in-memory identity and payload", async () => {
  let attempts = 0;
  const keys = [];
  const coordinator = createActivationCoordinator({ createKey: async () => "s".repeat(43) });
  const payload = { username: "member", password: "secret-password", name: "Member", email: "member@example.com" };
  const execute = async (key) => {
    keys.push(key);
    attempts += 1;
    if (attempts === 1) throw Object.assign(new Error("timeout"), { name: "TimeoutError" });
    return { state: "COMMITTED", value: { access_token: "token", user: { user_id: "user-1" } } };
  };

  await assert.rejects(coordinator.run({ kind: "SIGNUP", payload, execute }), /timeout/);
  await coordinator.run({ kind: "SIGNUP", payload: { ...payload }, execute });
  assert.deepEqual(keys, ["s".repeat(43), "s".repeat(43)]);
});

test("activation coordinator clears sensitive retry identity at an auth boundary", async () => {
  let keyIndex = 0;
  const coordinator = createActivationCoordinator({ createKey: async () => String(++keyIndex).repeat(43) });
  const failed = async () => { throw new Error("response lost"); };

  await assert.rejects(coordinator.run({
    kind: "SIGNUP",
    payload: { username: "old", password: "old-secret" },
    execute: failed,
  }), /response lost/);
  coordinator.clear();

  const keys = [];
  await coordinator.run({
    kind: "SIGNUP",
    payload: { username: "new", password: "new-secret" },
    execute: async (key) => { keys.push(key); return { state: "COMMITTED", value: {} }; },
  });
  await coordinator.run({
    kind: "ROOM_CREATE",
    payload: { name: "new account room" },
    execute: async (key) => { keys.push(key); return { state: "COMMITTED", value: {} }; },
  });

  assert.deepEqual(keys, ["2".repeat(43), "3".repeat(43)]);
});

test("concurrent same-kind activation calls share one key allocation", async () => {
  let keyAllocations = 0;
  let releaseKey;
  const keyGate = new Promise((resolve) => { releaseKey = resolve; });
  const coordinator = createActivationCoordinator({
    createKey: async () => {
      keyAllocations += 1;
      await keyGate;
      return "c".repeat(43);
    },
  });
  const keys = [];
  const execute = async (key) => {
    keys.push(key);
    return { state: "COMMITTED", value: {} };
  };
  const first = coordinator.run({ kind: "ROOM_CREATE", payload: { name: "Family" }, execute });
  const second = coordinator.run({ kind: "ROOM_CREATE", payload: { name: "Family" }, execute });
  releaseKey();
  await Promise.all([first, second]);

  assert.equal(keyAllocations, 1);
  assert.deepEqual(keys, ["c".repeat(43)]);
});

test("clearing the coordinator invalidates an in-flight activation result", async () => {
  let release;
  const response = new Promise((resolve) => { release = resolve; });
  const coordinator = createActivationCoordinator({ createKey: async () => "x".repeat(43) });
  const pending = coordinator.run({
    kind: "SIGNUP",
    payload: { username: "old", password: "secret" },
    execute: async () => response,
  });
  coordinator.clear();
  release({ state: "COMMITTED", value: { access_token: "old-token" } });
  await assert.rejects(pending, (error) => error?.code === "ACTIVATION_ABANDONED");
});

test("controller clears activation on auth loss and preserves matching scope on login", () => {
  const source = readFileSync(join(__dirname, "src/navigation/useAppController.js"), "utf8");
  assert.match(source, /setRuntimeUnauthorizedHandler[\s\S]*?activationCoordinator\.clear\(\);[\s\S]*?try \{[\s\S]*?authSession\.clear\(\)/);
  assert.match(source, /async function handleLogout[\s\S]*?activationCoordinator\.clear\(\);/);
  assert.match(source, /authSession\.login\([\s\S]*?activationCoordinator\.changeSession\([\s\S]*?sessionEstablished = true/);
  assert.match(source, /authSession\.establish\(activation\.value\);[\s\S]*?activationCoordinator\.clear\(\);/);
});
