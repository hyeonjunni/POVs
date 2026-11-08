const { test } = require("node:test");
const assert = require("node:assert/strict");
const { ApiError, createApiClient } = require("./apiClient.cjs");
const { createRecordsApi } = require("./records.cjs");

function fixture(respond = () => ({ record_id: "record/1", record_version: 8 })) {
  const calls = [];
  const api = createRecordsApi(createApiClient({
    baseUrl: "https://records.invalid/api/v1",
    getAccessToken: () => "fixture-token",
    fetchImpl: async (url, options) => {
      calls.push({ url, ...options, json: options.body ? JSON.parse(options.body) : undefined });
      const value = await respond(calls.at(-1), calls.length);
      return value instanceof Response ? value : new Response(JSON.stringify(value), { status: 200 });
    },
  }));
  return { api, calls };
}

const mutation = { recordVersion: 7, reason: "원문과 공개 범위를 확인했습니다.", idempotencyKey: "review-action-1" };

test("record scope is explicit when selected, preserving canonical permissions and revisions", async () => {
  const record = { record_id: "r", visibility: "PRIVATE", record_version: 9, capabilities: { can_read: true, can_soft_delete: false } };
  const { api, calls } = fixture(() => ({ items: [record], next_cursor: null }));
  for (const scope of [undefined, "PERSONAL", "FAMILY", "COMBINED"]) {
    assert.deepEqual(await api.list("room/1", { scope }), [record]);
    assert.equal(calls.at(-1).url, `https://records.invalid/api/v1/families/room%2F1/records${scope ? `?scope=${scope}` : ""}`);
  }
  await assert.rejects(api.list("room", { scope: "DELETED" }), /범위/);
  assert.equal(calls.length, 4);
});

test("management and revision history use known record IDs with encoded paths", async () => {
  const history = [{ changed_fields: ["title"], metadata_revision: 2 }];
  const { api, calls } = fixture(call => call.url.endsWith("metadata-revisions")
    ? { items: history, next_cursor: null }
    : { record_id: "record/1", lifecycle_state: "SOFT_DELETED", restore_deadline: "2026-11-10", capabilities: { can_restore: true } });
  const managed = await api.getManagement("record/1");
  assert.equal(managed.capabilities.can_restore, true);
  assert.equal(managed.restore_deadline, "2026-11-10");
  assert.deepEqual(await api.listMetadataRevisions("record/1"), history);
  assert.ok(calls[0].url.endsWith("/records/record%2F1/management"));
  assert.ok(calls[1].url.endsWith("/records/record%2F1/metadata-revisions"));
  assert.ok(calls.every(call => call.method === "GET"));
});

test("approval, visibility, and lifecycle actions carry exact reasons, version preconditions, and caller request keys", async () => {
  const { api, calls } = fixture();
  assert.equal((await api.approveReview("record/1", mutation)).record_version, 8);
  await api.changeVisibility("record/1", { ...mutation, visibility: "PRIVATE", idempotencyKey: "visibility-action-1" });
  await api.changeVisibility("record/1", { ...mutation, visibility: "CUSTOM", customGrants: ["member-2"], idempotencyKey: "visibility-action-2" });
  await api.transitionLifecycle("record/1", { ...mutation, action: "SOFT_DELETE", idempotencyKey: "delete-action-1" });
  assert.deepEqual(calls.map(call => [call.url.split("/records/")[1], call.json, call.headers["Idempotency-Key"]]), [
    ["record%2F1/reviews", { reason: mutation.reason }, "review-action-1"],
    ["record%2F1/visibility-changes", { visibility: "PRIVATE", custom_grants: [], reason: mutation.reason }, "visibility-action-1"],
    ["record%2F1/visibility-changes", { visibility: "CUSTOM", custom_grants: ["member-2"], reason: mutation.reason }, "visibility-action-2"],
    ["record%2F1/lifecycle-transitions", { action: "SOFT_DELETE", reason: mutation.reason }, "delete-action-1"],
  ]);
  for (const call of calls) {
    assert.equal(call.method, "POST");
    assert.equal(call.headers["If-Match"], '"7"');
    assert.equal(call.headers.Authorization, "Bearer fixture-token");
    assert.ok(call.signal instanceof AbortSignal);
  }
});

test("a transient response can be retried only by the caller using the same action and idempotency key", async () => {
  const { api, calls } = fixture((_call, count) => count === 1
    ? new Response(JSON.stringify({ detail: { code: "TEMPORARILY_UNAVAILABLE" } }), { status: 503 })
    : { record_id: "r", record_version: 8 });
  await assert.rejects(api.approveReview("r", mutation), error => error.status === 503);
  assert.equal(calls.length, 1, "the adapter must not silently repeat a mutation");
  await api.approveReview("r", mutation);
  assert.equal(calls[0].body, calls[1].body);
  assert.equal(calls[0].headers["Idempotency-Key"], calls[1].headers["Idempotency-Key"]);
  assert.equal(calls[0].headers["If-Match"], calls[1].headers["If-Match"]);
});

test("metadata PATCH sends only supported changed fields and never pretends to support idempotency replay", async () => {
  const { api, calls } = fixture();
  const changes = { title: "여행 기록", occurred_at: "2026-10-10", location: "강릉", category: null };
  await api.patchMetadata("record/1", { ...mutation, changes });
  assert.equal(calls[0].method, "PATCH");
  assert.ok(calls[0].url.endsWith("/records/record%2F1/metadata"));
  assert.deepEqual(calls[0].json, { changes, reason: mutation.reason });
  assert.equal(calls[0].headers["If-Match"], '"7"');
  assert.equal(calls[0].headers["Idempotency-Key"], undefined);
  assert.throws(() => api.patchMetadata("r", { ...mutation, changes: { description: "unsupported" } }), /수정할 수 없는/);
  assert.throws(() => api.patchMetadata("r", { ...mutation, changes: { tags: ["unsupported"] } }), /수정할 수 없는/);
  assert.equal(calls.length, 1);
});

test("conflict, stale-version, and permission errors survive unchanged for UI recovery without retries", async () => {
  const failureCases = [[412, "REVISION_CONFLICT"], [428, "PRECONDITION_REQUIRED"], [403, "ACTION_NOT_ALLOWED"], [409, "IDEMPOTENCY_CONFLICT"]];
  for (const [status, code] of failureCases) {
    const { api, calls } = fixture(() => new Response(JSON.stringify({ detail: { code } }), { status }));
    await assert.rejects(api.changeVisibility("r", { ...mutation, visibility: "FAMILY" }), error => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, status);
      assert.equal(error.code, code);
      assert.deepEqual(error.detail, { code });
      return true;
    });
    assert.equal(calls.length, 1);
  }
});

test("incomplete or invalid mutation identities are rejected before sending any request", () => {
  const { api, calls } = fixture();
  for (const recordVersion of [undefined, null, 0, -1, 1.5, "7", '"7"']) {
    assert.throws(() => api.approveReview("r", { ...mutation, recordVersion }), /버전/);
  }
  for (const idempotencyKey of [undefined, "", " ", "a".repeat(201)]) {
    assert.throws(() => api.approveReview("r", { ...mutation, idempotencyKey }), /식별자/);
  }
  for (const reason of [undefined, "", " ", "a".repeat(2001)]) {
    assert.throws(() => api.approveReview("r", { ...mutation, reason }), /사유/);
  }
  assert.throws(() => api.changeVisibility("r", { ...mutation, visibility: "PRIVATE", customGrants: ["member-1"] }), /선택한 가족/);
  assert.throws(() => api.transitionLifecycle("r", { ...mutation, action: "PURGE" }), /상태 변경/);
  assert.throws(() => api.patchMetadata("r", { ...mutation, changes: {} }), /수정할 기록/);
  assert.equal(calls.length, 0);
});

test("malformed list and revision responses cannot be mistaken for an empty result", async () => {
  const { api } = fixture(() => ({ items: null }));
  await assert.rejects(api.list("room"), /목록 응답/);
  await assert.rejects(api.listMetadataRevisions("record"), /이력 응답/);
});
