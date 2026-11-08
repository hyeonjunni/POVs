const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createPreparationFlow, mergeRecordDetails, recordStatus, recordMatchesScope, canPrepare } = require("./src/features/records/recordManagement.cjs");

function fixture(options = {}) {
  const calls = [], states = [];
  let active = true;
  const record = { id: "record", record_id: "record", room_id: "room", type: "text", title: "원문", detail: "텍스트 원문", hasFile: true, fileId: "file", record_version: 3,
    visibility: "PRIVATE", lifecycle_state: "ACTIVE", review_status: "PENDING_REVIEW", capabilities: { can_read: true, can_edit_content: true }, ...options.record };
  let job = { job_id: "job", record_id: "record", room_id: "room", upload_file_id: "file", processor: "text", status: "PENDING", attempt: 1, result: { text: "텍스트 원문" }, ...options.job };
  let review = { job_id: "job", review_status: "unreviewed", source_attempt: 1, source_result_sha256: "hash-A", user_confirmed: false, ...options.review };
  const canonical = () => {
    const { id, type, detail, fileId, hasFile, ...response } = record;
    return { ...response, description: detail };
  };
  const records = {
    async get() { calls.push("get-record"); if (options.afterGet) options.afterGet(); return canonical(); },
    async approveReview(id, payload) { calls.push(["approve-record", id, payload]); record.review_status = "APPROVED"; record.record_version++; return canonical(); },
  };
  const uploads = { async publishText() { calls.push("publish-text"); record.hasFile = true; return { upload_id: "record", has_file: true, file_id: "file" }; } };
  const processing = {
    async create(id, payload) { calls.push(["create-job", id, payload]); return { ...job }; },
    async get() { calls.push("get-job"); return { ...job }; },
    async run() { calls.push("run"); job.status = "SUCCEEDED"; if (options.failRun) throw options.failRun; return { ...job }; },
    async retry() { calls.push("retry"); job.status = "PENDING"; return { ...job }; },
    async getReview() { calls.push("get-review"); return { ...review }; },
    async review(id, action) { calls.push(["review", id, action]); review.review_status = action === "approve" ? "confirmed" : "rejected"; review.user_confirmed = action === "approve";
      if (options.changeAfterReview) { job.attempt++; review.source_attempt++; review.source_result_sha256 = "hash-B"; } },
    async index(id, jobId) { calls.push(["index", id, jobId]); if (options.failIndex) { const error = options.failIndex; options.failIndex = null; throw error; }
      return { chunk_count: 1, embedding_count: 1, source_attempt: review.source_attempt, source_result_sha256: review.source_result_sha256, ...options.index }; },
  };
  const flow = createPreparationFlow({ records, uploads, processing, roomId: "room", createKey: () => "request-key", assertCurrent() { if (!active) throw new Error("abandoned"); }, onState: value => states.push(value) });
  return { flow, record, calls, states, end: () => { active = false; }, replaceReview: value => { review = { ...review, ...value }; }, replaceJob: value => { job = { ...job, ...value }; } };
}

test("text source approval and analysis never automatically approve extraction or index", async () => {
  const h = fixture();
  const result = await h.flow.start(h.record);
  assert.equal(result.job.status, "SUCCEEDED");
  assert.equal(result.review.review_status, "unreviewed");
  const approval = h.calls.find(call => call[0] === "approve-record");
  assert.equal(approval[2].recordVersion, 3);
  assert.equal(approval[2].idempotencyKey, "request-key");
  assert.deepEqual(h.calls.find(call => call[0] === "create-job"), ["create-job", "record", { upload_file_id: "file", processor: "text" }]);
  assert.equal(h.calls.some(call => ["review", "index"].includes(call[0])), false);
  const done = await h.flow.decide("approve");
  assert.equal(done.indexed, true);
  assert.deepEqual(h.calls.filter(call => call[0] === "index"), [["index", "record", "job"]]);
});

test("rejecting extraction never publishes search evidence", async () => {
  const h = fixture(); await h.flow.start(h.record); await h.flow.decide("reject");
  assert.equal(h.calls.some(call => call[0] === "index"), false);
  await assert.rejects(h.flow.decide("index"), /승인 여부/);
});

test("partial text save resumes same record and source before approval", async () => {
  const h = fixture({ record: { hasFile: false, fileId: null } });
  const result = await h.flow.start(h.record);
  assert.equal(result.record.fileId, "file");
  assert.equal(h.calls.filter(call => call === "publish-text").length, 1);
  assert.ok(h.calls.indexOf("publish-text") < h.calls.findIndex(call => call[0] === "approve-record"));
});

test("non-owner, wrong room, and unsupported types cannot mutate", async () => {
  for (const override of [{ capabilities: { can_read: true, can_edit_content: false } }, { room_id: "other" }, { lifecycle_state: "ARCHIVED" }, { type: "video" }]) {
    const h = fixture({ record: override }); await assert.rejects(h.flow.start(h.record));
    assert.deepEqual(h.calls, ["get-record"]);
  }
});

test("late response after leaving session cannot start approval", async () => {
  let h;
  h = fixture({ afterGet: () => h.end() });
  await assert.rejects(h.flow.start(h.record), /abandoned/);
  assert.deepEqual(h.calls, ["get-record"]);
  assert.equal(h.states.length, 0);
});

test("different record, room, processor or file job is rejected", async () => {
  for (const job of [{ record_id: "other" }, { room_id: "other" }, { processor: "stt" }, { upload_file_id: "old-file" }]) {
    const h = fixture({ job, record: { review_status: "APPROVED" } }); await assert.rejects(h.flow.start(h.record), /원본/);
    assert.equal(h.calls.includes("run"), false);
  }
});

test("failed jobs require explicit retry; repeated starts reuse the same tuple", async () => {
  const h = fixture({ job: { status: "FAILED" } });
  await h.flow.start(h.record); assert.equal(h.calls.includes("retry"), false);
  await h.flow.start(h.record, { retry: true }); assert.equal(h.calls.filter(call => call === "retry").length, 1);
  assert.equal(h.calls.filter(call => call[0] === "approve-record").length, 1);
});

test("lost run response is recovered by the same job without another run", async () => {
  const h = fixture({ failRun: new Error("timeout") });
  await assert.rejects(h.flow.start(h.record), /timeout/);
  const current = await h.flow.start(h.record);
  assert.equal(current.job.status, "SUCCEEDED");
  assert.equal(h.calls.filter(call => call === "run").length, 1);
});

test("result replacement before or during approval prevents indexing", async () => {
  for (const changeAfterReview of [false, true]) {
    const h = fixture({ changeAfterReview }); await h.flow.start(h.record);
    if (!changeAfterReview) { h.replaceJob({ attempt: 2 }); h.replaceReview({ source_attempt: 2, source_result_sha256: "hash-B" }); }
    await assert.rejects(h.flow.decide("approve"), /변경/);
    assert.equal(h.calls.some(call => call[0] === "index"), false);
  }
});

test("only positive counts with matching provenance can claim ready", async () => {
  for (const index of [{ source_attempt: 2 }, { source_result_sha256: "other" }, { chunk_count: 0 }, { embedding_count: 0 }]) {
    const h = fixture({ index }); await h.flow.start(h.record); await assert.rejects(h.flow.decide("approve"));
    assert.equal(h.states.some(state => state.indexed), false);
  }
});

test("index failure retains approval and retry never repeats approval write", async () => {
  const h = fixture({ failIndex: new Error("unavailable") }); await h.flow.start(h.record);
  await assert.rejects(h.flow.decide("approve"), /unavailable/);
  assert.equal((await h.flow.decide("index")).indexed, true);
  assert.equal(h.calls.filter(call => call[0] === "review").length, 1);
});

test("canonical permissions/version survive mapping; review approval is not search readiness", () => {
  const h = fixture();
  const mapped = mergeRecordDetails(h.record, { ...h.record, record_version: 7, review_status: "APPROVED" });
  assert.equal(mapped.record_version, 7); assert.equal(canPrepare(mapped), true);
  assert.equal(recordStatus(mapped), "기록 승인됨");
  assert.equal(recordMatchesScope(mapped, "PERSONAL"), true);
  assert.equal(recordMatchesScope(mapped, "FAMILY"), false);
  assert.throws(() => mergeRecordDetails(h.record, { record_id: "other" }));
});
