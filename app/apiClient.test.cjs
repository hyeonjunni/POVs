const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  ApiError,
  createApiClient,
  normalizeApiBaseUrl,
  resolveApiBaseUrl,
} = require("./apiClient.cjs");

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  };
}

test("normalizes one API base path without leaking URL credentials or query data", () => {
  assert.equal(normalizeApiBaseUrl("127.0.0.1:8001/"), "http://127.0.0.1:8001/api/v1");
  assert.equal(normalizeApiBaseUrl("http://127.0.0.1:8001/api/v1/"), "http://127.0.0.1:8001/api/v1");
  assert.throws(() => normalizeApiBaseUrl("https://user:pass@example.com"), /API base URL/);
  assert.throws(() => normalizeApiBaseUrl("https://example.com?access_token=secret"), /API base URL/);
});

test("uses the Expo public API override as the effective startup URL", () => {
  assert.equal(
    resolveApiBaseUrl("https://frontend.example/api/v1/"),
    "https://frontend.example/api/v1"
  );
});

test("sends bearer auth in headers while keeping the token out of the URL", async () => {
  const calls = [];
  const token = "synthetic+/?=&token";
  const client = createApiClient({
    baseUrl: "http://127.0.0.1:8001",
    getAccessToken: () => token,
    fetchImpl: async (...args) => {
      calls.push(args);
      return jsonResponse(200, { user_id: "user-1" });
    },
  });

  assert.deepEqual(await client.get("/auth/me"), { user_id: "user-1" });
  assert.equal(calls[0][0], "http://127.0.0.1:8001/api/v1/auth/me");
  assert.equal(calls[0][1].headers.Authorization, `Bearer ${token}`);
  assert.ok(!calls[0][0].includes(token));
  assert.ok(!calls[0][0].includes("access_token"));
});

test("serializes JSON but leaves multipart content type to the native runtime", async () => {
  const calls = [];
  const client = createApiClient({
    baseUrl: "http://127.0.0.1:8001/api/v1",
    getAccessToken: () => "token",
    fetchImpl: async (...args) => {
      calls.push(args);
      return jsonResponse(200, { ok: true });
    },
  });

  await client.post("/uploads", { json: { room_id: "room-1", type: "image", title: "photo" } });
  const multipart = { append() {} };
  await client.post("/uploads/upload-1/binary", { body: multipart });

  assert.equal(calls[0][1].headers["Content-Type"], "application/json");
  assert.equal(calls[0][1].body, JSON.stringify({ room_id: "room-1", type: "image", title: "photo" }));
  assert.equal(calls[1][1].headers["Content-Type"], undefined);
  assert.equal(calls[1][1].body, multipart);
});

test("turns API failures into typed errors and clears the session only for 401", async () => {
  const cleared = [];
  const responses = [
    jsonResponse(401, { detail: "expired", code: "TOKEN_EXPIRED" }),
    jsonResponse(403, { detail: "forbidden" }),
  ];
  const client = createApiClient({
    baseUrl: "http://127.0.0.1:8001",
    getAccessToken: () => "token",
    onUnauthorized: async () => cleared.push("cleared"),
    fetchImpl: async () => responses.shift(),
  });

  await assert.rejects(client.get("/auth/me"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 401);
    assert.equal(error.code, "TOKEN_EXPIRED");
    return true;
  });
  await assert.rejects(client.get("/families"), (error) => error.status === 403);
  assert.deepEqual(cleared, ["cleared"]);
});

test("reports the token that originated a delayed 401", async () => {
  let currentToken = "token-a";
  let finish;
  const unauthorizedTokens = [];
  const client = createApiClient({
    baseUrl: "http://127.0.0.1:8001",
    getAccessToken: () => currentToken,
    onUnauthorized: (requestToken) => unauthorizedTokens.push(requestToken),
    fetchImpl: () => new Promise((resolve) => { finish = resolve; }),
  });

  const pending = client.get("/auth/me");
  await Promise.resolve();
  currentToken = "token-b";
  finish(jsonResponse(401, { detail: "expired" }));

  await assert.rejects(pending, (error) => error.status === 401);
  assert.deepEqual(unauthorizedTokens, ["token-a"]);
});

test("503 shows a readable recovery message while preserving HTTP details and the session", async () => {
  const cleared = [], calls = [];
  const responses = [
    jsonResponse(503, {detail: "secure_request_unavailable"}),
    jsonResponse(503, {detail: {code: "TEMPORARILY_UNAVAILABLE", retryable: true}}),
  ];
  const client = createApiClient({
    baseUrl: "http://backend.example/api/v1",
    getAccessToken: () => "fixture-token",
    onUnauthorized: token => cleared.push(token),
    fetchImpl: async (url, options) => { calls.push({url, options}); return responses.shift(); },
  });
  await assert.rejects(client.get("/records/record-1"), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 503);
    assert.equal(error.code, null);
    assert.equal(error.detail, "secure_request_unavailable");
    assert.match(error.message, /서버.*최신 상태를 확인/);
    assert.doesNotMatch(error.message, /secure_request_unavailable/);
    return true;
  });
  await assert.rejects(client.post("/records/record-1/reviews", {json: {reason: "fixture"}}), error => {
    assert.equal(error.status, 503);
    assert.equal(error.code, "TEMPORARILY_UNAVAILABLE");
    assert.deepEqual(error.detail, {code: "TEMPORARILY_UNAVAILABLE", retryable: true});
    assert.match(error.message, /최신 상태를 확인/);
    return true;
  });
  assert.equal(calls.length, 2, "neither GET nor mutation is silently retried");
  assert.deepEqual(calls.map(call => call.options.method), ["GET", "POST"]);
  assert.deepEqual(cleared, [], "server unavailability must not log the user out");
});

test("other HTTP failures retain their original message and typed details", () => {
  const error = new ApiError({status: 413, code: "PAYLOAD_TOO_LARGE", detail: "secure_request_unavailable"});
  assert.equal(error.message, "secure_request_unavailable");
  assert.equal(error.status, 413);
  assert.equal(error.code, "PAYLOAD_TOO_LARGE");
  assert.equal(error.detail, "secure_request_unavailable");
});
