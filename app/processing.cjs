const TERMINAL_STATES = new Set(["SUCCEEDED", "FAILED"]);

function abortError() {
  const error = new Error("Processing polling cancelled");
  error.name = "AbortError";
  return error;
}

function timeoutError() {
  return Object.assign(new Error("요청 시간이 초과되었습니다. 서버에서는 처리가 완료되었을 수 있습니다. 상태를 다시 확인해주세요."), { code: "REQUEST_TIMEOUT" });
}

async function requestWithDeadline(request, { signal = null, timeoutMs }) {
  if (signal?.aborted) throw abortError();
  const controller = new AbortController();
  let timer;
  let abortListener;
  const boundary = new Promise((_, reject) => {
    timer = setTimeout(() => {
      reject(timeoutError());
      controller.abort();
    }, Math.max(0, timeoutMs));
    if (signal) {
      abortListener = () => {
        controller.abort();
        reject(abortError());
      };
      signal.addEventListener("abort", abortListener, { once: true });
    }
  });
  try {
    return await Promise.race([request(controller.signal), boundary]);
  } finally {
    clearTimeout(timer);
    if (abortListener) signal.removeEventListener("abort", abortListener);
  }
}

function createProcessingApi(client, { timeoutMs = 15000 } = {}) {
  const rawClient = client;
  client = Object.fromEntries(["get", "post"].map(method => [method, (path, options = {}) =>
    options.signal ? rawClient[method](path, options) : requestWithDeadline(signal => rawClient[method](path, { ...options, signal }), {
      timeoutMs, signal: options.signal,
    })]));
  const jobPath = (jobId, suffix = "") => `/processing-jobs/${encodeURIComponent(jobId)}${suffix}`;
  return {
    create: (recordId, payload) => client.post(
      `/records/${encodeURIComponent(recordId)}/processing-jobs`,
      { json: payload }
    ),
    get: (jobId, options = {}) => Object.keys(options).length
      ? requestWithDeadline(
        (requestSignal) => client.get(jobPath(jobId), { signal: requestSignal }),
        options
      )
      : client.get(jobPath(jobId)),
    run: (jobId, options = {}) => Object.keys(options).length
      ? requestWithDeadline(
        (requestSignal) => client.post(jobPath(jobId, "/run"), { signal: requestSignal }),
        options
      )
      : client.post(jobPath(jobId, "/run"), {}),
    retry: (jobId, options = {}) => Object.keys(options).length
      ? requestWithDeadline(
        (requestSignal) => client.post(jobPath(jobId, "/retry"), { signal: requestSignal }),
        options
      )
      : client.post(jobPath(jobId, "/retry"), {}),
    getReview: (jobId) => client.get(jobPath(jobId, "/review")),
    review: (jobId, action) => client.post(jobPath(jobId, "/review"), { json: { action } }),
    // Indexing waits for the server's embedding provider and publication; its
    // allowed processing time exceeds the ordinary 15-second API deadline.
    index: (uploadId, jobId, options = {}) => requestWithDeadline(
      signal => client.post(`/records/${encodeURIComponent(uploadId)}/retrieval-index`,
        { json: { processing_job_id: jobId }, expectedStatus: 200, signal }),
      { timeoutMs: 120000, ...options }
    ),
    async poll(jobId, {
      intervalMs = 1000,
      timeoutMs = 60000,
      signal = null,
      sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    } = {}) {
      const startedAt = Date.now();
      const deadline = startedAt + timeoutMs;
      while (Date.now() <= deadline) {
        if (signal?.aborted) throw abortError();
        const remainingMs = deadline - Date.now();
        if (remainingMs <= 0) throw timeoutError();
        const job = await this.get(jobId, { signal, timeoutMs: remainingMs });
        if (TERMINAL_STATES.has(job.status)) return job;
        await requestWithDeadline(() => sleep(intervalMs), {
          signal,
          timeoutMs: Math.max(1, deadline - Date.now()),
        });
      }
      throw timeoutError();
    },
  };
}

function createOcrReviewFlow(processingApi) {
  let context = null;
  let generation = 0;

  function abandonedError() {
    const error = new Error("이전 세션의 OCR 검토 요청이 취소되었습니다.");
    error.code = "OCR_REVIEW_ABANDONED";
    return error;
  }

  function clear() {
    generation += 1;
    context = null;
  }

  function setContext(job) {
    generation += 1;
    context = { job, generation };
    return context;
  }

  function getContext() {
    return context;
  }

  async function reload() {
    if (!context?.job?.job_id) throw new Error("OCR 검토 작업이 없습니다.");
    const snapshot = context;
    const review = await processingApi.getReview(snapshot.job.job_id);
    if (context !== snapshot || generation !== snapshot.generation) throw abandonedError();
    snapshot.needsReload = false;
    const result = { ...snapshot, review };
    if (["confirmed", "rejected"].includes(review?.review_status)) context = null;
    return result;
  }

  async function start(job) {
    setContext(job);
    return reload();
  }

  async function decide(action) {
    if (!context?.job?.job_id) throw new Error("OCR 검토 작업이 없습니다.");
    if (context.needsReload) {
      const current = await reload();
      if (current.review.review_status !== "unreviewed") return current;
    }
    const snapshot = context;
    try {
      snapshot.action = action;
      await processingApi.review(snapshot.job.job_id, action);
    } catch (error) {
      snapshot.needsReload = true;
      if (error?.status !== 409) throw error;
    }
    if (context !== snapshot || generation !== snapshot.generation) throw abandonedError();
    snapshot.needsReload = true;
    const result = await reload();
    return result;
  }

  return { clear, decide, getContext, reload, setContext, start };
}

module.exports = { TERMINAL_STATES, createOcrReviewFlow, createProcessingApi, requestWithDeadline };
