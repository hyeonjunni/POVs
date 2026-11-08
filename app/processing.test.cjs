const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createOcrReviewFlow, createProcessingApi } = require("./processing.cjs");

test("creates and runs OCR jobs with exact backend DTOs", async () => {
  const calls = [];
  const client = { post: async (...args) => { calls.push(args); return { job_id: "job-1" }; } };
  const processing = createProcessingApi(client);

  await processing.create("record/1", { upload_file_id: "file-1", processor: "ocr" });
  await processing.run("job-1");

  for (const [, options] of calls) { assert.ok(options.signal instanceof AbortSignal); delete options.signal; }
  assert.deepEqual(calls, [
    ["/records/record%2F1/processing-jobs", { json: { upload_file_id: "file-1", processor: "ocr" } }],
    ["/processing-jobs/job-1/run", {}],
  ]);
});

test("OCR review API loads and submits approve or reject actions", async () => {
  const calls = [];
  const processing = createProcessingApi({
    get: async (...args) => { calls.push(["get", ...args]); return { review_status: "unreviewed" }; },
    post: async (...args) => { calls.push(["post", ...args]); return { review_status: args[1].json.action === "approve" ? "confirmed" : "rejected" }; },
  });

  assert.equal((await processing.getReview("job/1")).review_status, "unreviewed");
  assert.equal((await processing.review("job/1", "approve")).review_status, "confirmed");
  assert.equal((await processing.review("job/1", "reject")).review_status, "rejected");
  for (const [, , options] of calls) { assert.ok(options.signal instanceof AbortSignal); delete options.signal; }
  assert.deepEqual(calls, [
    ["get", "/processing-jobs/job%2F1/review", {}],
    ["post", "/processing-jobs/job%2F1/review", { json: { action: "approve" } }],
    ["post", "/processing-jobs/job%2F1/review", { json: { action: "reject" } }],
  ]);
});

test("OCR review preserves load failures and stale conflict responses for the UI", async () => {
  const loadError = Object.assign(new Error("review unavailable"), { status: 503 });
  const conflictError = Object.assign(new Error("already reviewed"), { status: 409 });
  const processing = createProcessingApi({
    get: async () => { throw loadError; },
    post: async () => { throw conflictError; },
  });

  await assert.rejects(processing.getReview("job-1"), (error) => error === loadError);
  await assert.rejects(processing.review("job-1", "approve"), (error) => error === conflictError);
});

test("OCR review load retry retains and reuses the same job", async () => {
  const calls = [];
  let fail = true;
  const flow = createOcrReviewFlow({
    getReview: async (jobId) => {
      calls.push(jobId);
      if (fail) {
        fail = false;
        throw Object.assign(new Error("unavailable"), { status: 503 });
      }
      return { job_id: jobId, review_status: "unreviewed" };
    },
    review: async () => {},
  });

  await assert.rejects(flow.start({ job_id: "job-1", result: { text: "hello" } }), /unavailable/);
  assert.equal(flow.getContext().job.job_id, "job-1");
  assert.equal((await flow.reload()).review.job_id, "job-1");
  assert.deepEqual(calls, ["job-1", "job-1"]);
});

for (const action of ["approve", "reject"]) {
  test(`OCR ${action} retry reloads the same job after a transient failure`, async () => {
    const calls = [];
    let fail = true;
    const flow = createOcrReviewFlow({
      getReview: async (jobId) => ({ job_id: jobId, review_status: action === "approve" ? "confirmed" : "rejected" }),
      review: async (jobId, nextAction) => {
        calls.push([jobId, nextAction]);
        if (fail) {
          fail = false;
          throw Object.assign(new Error("temporary"), { status: 503 });
        }
      },
    });
    flow.setContext({ job_id: "job-2", result: { text: "hello" } });

    await assert.rejects(flow.decide(action), /temporary/);
    assert.equal(flow.getContext().job.job_id, "job-2");
    const result = await flow.decide(action);
    assert.equal(result.review.review_status, action === "approve" ? "confirmed" : "rejected");
    assert.deepEqual(calls, [["job-2", action]]);
    assert.equal(flow.getContext(), null);
  });

  test(`OCR ${action} conflict refreshes the same job's authoritative state`, async () => {
    const calls = [];
    const flow = createOcrReviewFlow({
      getReview: async (jobId) => {
        calls.push(["get", jobId]);
        return { job_id: jobId, review_status: action === "approve" ? "confirmed" : "rejected" };
      },
      review: async (jobId, nextAction) => {
        calls.push(["post", jobId, nextAction]);
        throw Object.assign(new Error("already reviewed"), { status: 409 });
      },
    });
    flow.setContext({ job_id: "job-conflict", result: { text: "hello" } });

    const result = await flow.decide(action);

    assert.equal(result.review.review_status, action === "approve" ? "confirmed" : "rejected");
    assert.deepEqual(calls, [["post", "job-conflict", action], ["get", "job-conflict"]]);
    assert.equal(flow.getContext(), null);
  });
}

test("clearing an OCR review flow removes private job context at the session boundary", () => {
  const flow = createOcrReviewFlow({ getReview: async () => {}, review: async () => {} });
  flow.setContext({ job_id: "job-private", result: { text: "private OCR text" } });
  flow.clear();
  assert.equal(flow.getContext(), null);
});

test("a stale OCR response cannot overwrite a newer session's review context", async () => {
  let resolveOld;
  const oldResponse = new Promise((resolve) => { resolveOld = resolve; });
  const flow = createOcrReviewFlow({
    getReview: async (jobId) => jobId === "old-job" ? oldResponse : { job_id: jobId, review_status: "unreviewed" },
    review: async () => {},
  });
  const stale = flow.start({ job_id: "old-job", result: { text: "old private text" } });
  flow.clear();
  flow.setContext({ job_id: "new-job", result: { text: "new text" } });
  resolveOld({ job_id: "old-job", review_status: "confirmed" });

  await assert.rejects(stale, (error) => error?.code === "OCR_REVIEW_ABANDONED");
  assert.equal(flow.getContext().job.job_id, "new-job");
});

test("polling stops at SUCCEEDED and returns the terminal job", async () => {
  const states = [
    { job_id: "job-1", status: "PENDING" },
    { job_id: "job-1", status: "RUNNING" },
    { job_id: "job-1", status: "SUCCEEDED", result: { text: "hello" } },
  ];
  const processing = createProcessingApi({ get: async () => states.shift() });
  const result = await processing.poll("job-1", { intervalMs: 0, timeoutMs: 1000, sleep: async () => {} });
  assert.equal(result.status, "SUCCEEDED");
  assert.deepEqual(result.result, { text: "hello" });
  assert.equal(states.length, 0);
});

test("polling returns FAILED without retry and respects cancellation", async () => {
  const failed = createProcessingApi({ get: async () => ({ job_id: "job-1", status: "FAILED", failure_code: "OCR_FAILED" }) });
  assert.equal((await failed.poll("job-1", { intervalMs: 0, sleep: async () => {} })).status, "FAILED");

  const controller = new AbortController();
  controller.abort();
  await assert.rejects(failed.poll("job-1", { signal: controller.signal }), { name: "AbortError" });
});

test("mid-flight cancellation and timeout bound a hung status request", async () => {
  const signals = [];
  const processing = createProcessingApi({
    get: async (_path, options) => {
      signals.push(options.signal);
      return new Promise(() => {});
    },
  });
  const controller = new AbortController();
  const cancelled = processing.poll("job-1", { signal: controller.signal, timeoutMs: 1000 });
  setTimeout(() => controller.abort(), 5);
  await assert.rejects(cancelled, { name: "AbortError" });
  assert.equal(signals[0].aborted, true);

  await assert.rejects(
    processing.poll("job-2", { timeoutMs: 5 }),
    /초과/
  );
});

test("positive polling interval reaches SUCCEEDED without racing its sleep timer", async () => {
  const states = [
    { job_id: "job-1", status: "PENDING" },
    { job_id: "job-1", status: "SUCCEEDED" },
  ];
  const processing = createProcessingApi({ get: async () => states.shift() });
  const result = await processing.poll("job-1", { intervalMs: 20, timeoutMs: 200 });
  assert.equal(result.status, "SUCCEEDED");
});

test("retry request is bounded when the backend stalls", async () => {
  let requestSignal;
  const processing = createProcessingApi({
    post: async (_path, options) => {
      requestSignal = options.signal;
      return new Promise(() => {});
    },
  });
  await assert.rejects(processing.retry("job-1", { timeoutMs: 5 }), /초과/);
  assert.equal(requestSignal.aborted, true);
});
