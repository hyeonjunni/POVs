const DEFAULT_ASK = Object.freeze({ model_id: "gemma-4-31b-dgx", arm: "hybrid_rrf", k: 5 });
const GATES = new Set(["ANSWER", "REFUSE", "CLARIFY", "CONFLICT", "NO_RECORD", "NEEDS_DATA"]);
const BINDING_KEYS = ["expected_ciphertext_sha256", "file_id", "source_object_id", "source_object_version"];
const PROCESSING_KEYS = ["processing_job_id", "source_attempt", "source_result_sha256"];
const nonblank = value => typeof value === "string" && value.trim().length > 0;

function validateCitation(citation, baseUrl) {
  const textKeys = ["fact_id", "chunk_id", "record_id", "file_id", "extractor", "extractor_id", "review_status", "original_url"];
  if (!citation || textKeys.some(key => !nonblank(citation[key])) ||
      !Number.isSafeInteger(citation.chunk_char_start) || citation.chunk_char_start < 0 ||
      !Number.isSafeInteger(citation.chunk_char_end) || citation.chunk_char_end <= citation.chunk_char_start ||
      citation.review_status === "rejected" ||
      citation.evidence_status !== "extracted" || typeof citation.user_confirmed !== "boolean" ||
      !citation.source_locator || typeof citation.source_locator !== "object" || Array.isArray(citation.source_locator)) {
    throw new Error("근거 응답 형식을 확인할 수 없습니다.");
  }
  let base, url;
  try { base = new URL(baseUrl); url = new URL(citation.original_url, base.origin); }
  catch (_) { throw new Error("원본의 근거 연결이 일치하지 않습니다."); }
  const keys = [...url.searchParams.keys()].sort();
  if (!nonblank(citation.source_object_id) ||
      !Number.isSafeInteger(citation.source_object_version) || citation.source_object_version < 1 ||
      !nonblank(citation.source_ciphertext_sha256) || !/^[a-f0-9]{64}$/.test(citation.source_ciphertext_sha256) ||
      "source_sha256" in citation) {
    throw new Error("근거 응답 형식을 확인할 수 없습니다.");
  }
  const expected = {
    expected_ciphertext_sha256: citation.source_ciphertext_sha256,
    file_id: citation.file_id,
    source_object_id: citation.source_object_id,
    source_object_version: String(citation.source_object_version),
  };
  const processing = PROCESSING_KEYS.some(key => url.searchParams.has(key));
  const epoch = url.searchParams.has("authorization_epoch");
  const claim = citation.claim_id != null || citation.claim_version != null ||
    url.searchParams.has("claim_id") || url.searchParams.has("claim_version");
  if (epoch && (!processing || !/^[1-9][0-9]*$/.test(url.searchParams.get("authorization_epoch") || "") ||
      !Number.isSafeInteger(Number(url.searchParams.get("authorization_epoch"))))) {
    throw new Error("원본의 근거 연결이 일치하지 않습니다.");
  }
  if (claim) {
    if (!nonblank(citation.claim_id) || !Number.isSafeInteger(citation.claim_version) || citation.claim_version < 1 || !processing) {
      throw new Error("원본의 주장 연결이 일치하지 않습니다.");
    }
    expected.claim_id = citation.claim_id;
    expected.claim_version = String(citation.claim_version);
  }
  const expectedKeys = [...BINDING_KEYS, ...(processing ? PROCESSING_KEYS : []),
    ...(epoch ? ["authorization_epoch"] : []), ...(claim ? ["claim_id", "claim_version"] : [])].sort();
  const expectedPath = `${base.pathname}/uploads/${encodeURIComponent(citation.record_id)}/content`;
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.search || base.hash ||
      !base.pathname.endsWith("/api/v1") || url.origin !== base.origin || url.username || url.password || url.hash ||
      url.pathname !== expectedPath ||
      keys.length !== expectedKeys.length || keys.some((key, index) => key !== expectedKeys[index]) ||
      expectedKeys.some(key => url.searchParams.getAll(key).length !== 1) ||
      Object.keys(expected).some(key => url.searchParams.get(key) !== expected[key]) ||
      (processing && (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(url.searchParams.get("processing_job_id") || "") ||
        !/^(0|[1-9][0-9]*)$/.test(url.searchParams.get("source_attempt") || "") ||
        !Number.isSafeInteger(Number(url.searchParams.get("source_attempt"))) ||
        !/^[a-f0-9]{64}$/.test(url.searchParams.get("source_result_sha256") || "")))) {
    throw new Error("원본의 근거 연결이 일치하지 않습니다.");
  }
  return url.toString();
}

function freezeJson(value) {
  if (value && typeof value === "object") { Object.values(value).forEach(freezeJson); Object.freeze(value); }
  return value;
}

function validateAskResponse(payload, baseUrl) {
  const generalSources = payload?.general_sources ?? [];
  const validGeneralSources = Array.isArray(generalSources) && generalSources.length <= 10 && generalSources.every(source => {
    try { const url = new URL(source); return url.protocol === "https:" && !url.username && !url.password; }
    catch (_) { return false; }
  });
  const generalAnswer = payload?.gate_action === "general_health_reference" && validGeneralSources && generalSources.length > 0;
  if (!payload || !GATES.has(payload.gate_route) || !Array.isArray(payload.citations) || payload.citations.length > 50 ||
      !validGeneralSources || (payload.gate_action === "general_health_reference" && !generalAnswer) ||
      !nonblank(payload.gate_action) || !nonblank(payload.provider_mode) ||
      (payload.answer !== null && typeof payload.answer !== "string") || (payload.answer?.length || 0) > 65536 ||
      (payload.gate_route === "ANSWER" && (!nonblank(payload.answer) || (!payload.citations.length && !generalAnswer) || (payload.provider_mode === "not_called" && !["structured_render", "claim_render", "record_discovery_render"].includes(payload.gate_action)))) ||
      (!["ANSWER", "NEEDS_DATA"].includes(payload.gate_route) && !(payload.gate_route === "CONFLICT" && payload.gate_action === "claim_render") && payload.citations.length)) {
    throw new Error("대화 응답 형식을 확인할 수 없습니다.");
  }
  payload.citations.forEach(citation => validateCitation(citation, baseUrl));
  const context = payload.query_context;
  if (context && (!["ROOM", "FOLLOWUP", "RECORD"].includes(context.scope) || !Array.isArray(context.records) ||
      context.records.length > 50 || context.records.some(record => typeof record.title !== "string" ||
        !payload.citations.some(citation => citation.record_id === record.record_id)))) {
    throw new Error("대화의 기록 범위를 확인할 수 없습니다.");
  }
  // Copy the immutable source tuple, including the locator, instead of retaining mutable transport objects.
  return freezeJson(JSON.parse(JSON.stringify({
    gate_route: payload.gate_route, answer: payload.answer, citations: payload.citations,
    gate_action: payload.gate_action, provider_mode: payload.provider_mode,
    structured_result: payload.structured_result ?? null, claim_result: payload.claim_result ?? null,
    query_context: context ?? null,
    general_sources: generalSources,
  })));
}

function createGroundedChatApi(api) {
  return {
    async ask({ roomId, query, history = [], selectedRecordId, contextMode, baseUrl, signal }) {
      const text = typeof query === "string" ? query.trim() : "";
      if (!nonblank(roomId) || !text || text.length > 4096) throw new Error("질문은 1~4096자로 입력해주세요.");
      const payload = await api.post(`/families/${encodeURIComponent(roomId)}/ask`, {
        json: { query: text, ...DEFAULT_ASK, ...(history.length ? { history } : {}),
          ...(contextMode ? { context_mode: contextMode } : {}),
          ...(selectedRecordId ? { selected_record_id: selectedRecordId } : {}) }, signal,
      });
      return validateAskResponse(payload, baseUrl);
    },
  };
}

function chatErrorMessage(error) {
  if (error?.code === "STALE_SOURCE") return "이 근거의 원본이 변경되었습니다. 최신 원본으로 바꾸지 않고 열람을 중단했습니다.";
  if (error?.name === "TimeoutError") return "답변을 기다리는 시간이 길어졌습니다. 잠시 후 다시 질문해주세요.";
  const messages = {
    401: "로그인이 만료되었습니다. 다시 로그인해주세요.",
    403: "이 가족방의 기록에 접근할 권한이 없습니다.",
    404: "가족방 또는 원본을 찾을 수 없습니다. 선택한 가족방을 확인해주세요.",
    409: "질문 처리 중 기록이 변경되었습니다. 다시 질문해주세요.",
    422: "질문 내용을 확인해주세요.",
    503: "대화 서버를 현재 사용할 수 없습니다. 잠시 후 다시 시도해주세요.",
  };
  if (Number.isInteger(error?.status) && error.status >= 400 && error.status <= 599) {
    return `${messages[error.status] || "요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요."} (HTTP ${error.status})`;
  }
  return error instanceof TypeError
    ? "서버에 연결하지 못했습니다. 네트워크 연결과 브라우저 접근 허용을 확인해주세요."
    : error?.message || "대화를 처리하지 못했습니다.";
}

function sameScope(left, right) {
  return ["userId", "roomId", "token", "baseUrl"].every(key => left?.[key] === right?.[key]);
}
function isRetryable(error) {
  if (error?.status === 409 || ["STALE_SOURCE", "GroundedEvidenceChanged"].includes(error?.code)) return false;
  return error?.name === "TimeoutError" || error instanceof TypeError || [408, 429, 502, 503, 504].includes(error?.status);
}
const emptyState = () => ({ draft: "", messages: [], sending: false, error: null, contextMode: "ROOM" });

function createChatSession({ readScope, ask, onChange = () => {}, timeoutMs = 150000 }) {
  let scope = null, state = emptyState(), sequence = 0, active = null, disposed = false;
  const emit = () => { if (!disposed) onChange(state); };
  function updateScope(next) {
    if (sameScope(scope, next)) return;
    active?.abort(); active = null;
    scope = { ...next }; state = emptyState(); emit();
  }
  function current(captured) { return !disposed && sameScope(scope, captured) && sameScope(readScope(), captured); }
  async function send(value, selectedRecordId, retryRequest) {
    updateScope(readScope());
    if (disposed || state.sending || !scope.userId || !scope.roomId || !scope.token) return false;
    const query = String(value ?? state.draft).trim();
    if (!query || query.length > 4096) return false;
    const captured = { ...scope }, id = `ask-${++sequence}`, controller = new AbortController();
    const contextMode = retryRequest?.contextMode ?? (selectedRecordId ? "ROOM" : state.contextMode);
    const history = retryRequest?.history ?? state.messages.filter(message => message.response?.gate_route === "ANSWER")
      .slice(-4).map(message => ({ query: message.query,
        record_ids: [...new Set(message.response.citations.map(citation => citation.record_id))].slice(0, 20) }));
    active = controller;
    // A retry submits a previous message, not the draft the user is composing.
    state = { ...state, draft: value == null ? "" : state.draft, sending: true, error: null,
      messages: [...state.messages, { id, query, request: { history, contextMode, selectedRecordId }, response: null, error: null }] }; emit();
    let timer;
    try {
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => { const error = new Error("Ask timed out"); error.name = "TimeoutError"; reject(error); controller.abort(); }, timeoutMs);
        controller.signal.addEventListener("abort", () => { const error = new Error("Ask cancelled"); error.name = "AbortError"; reject(error); }, { once: true });
      });
      const response = await Promise.race([ask({ ...captured, query, history, contextMode, selectedRecordId, signal: controller.signal }), deadline]);
      if (!current(captured) || active !== controller) return false;
      state = { ...state, contextMode: response.gate_route === "ANSWER" ? "FOLLOWUP" : state.contextMode,
        messages: state.messages.map(message => message.id === id ? { ...message, response } : message) };
      return true;
    } catch (error) {
      if (!current(captured) || active !== controller || error?.name === "AbortError") return false;
      const message = chatErrorMessage(error);
      state = { ...state, error: null,
        messages: state.messages.map(item => item.id === id ? { ...item, error: message, retryable: isRetryable(error) } : item) };
      return false;
    } finally {
      clearTimeout(timer);
      if (current(captured) && active === controller) { active = null; state = { ...state, sending: false }; emit(); }
    }
  }
  return {
    updateScope, send,
    reset() { active?.abort(); active = null; state = emptyState(); emit(); },
    setContextMode(value) { if (["ROOM", "FOLLOWUP"].includes(value) && !state.sending) { state = { ...state, contextMode: value }; emit(); } },
    connect() { disposed = false; emit(); },
    disconnect() {
      disposed = true; active?.abort(); active = null;
      state = { ...state, sending: false };
    },
    setDraft(value) { updateScope(readScope()); state = { ...state, draft: String(value).slice(0, 4096) }; emit(); },
    dismissError() { state = { ...state, error: null }; emit(); },
    retry(requested) { updateScope(readScope()); const failed = requested?.id ? state.messages.find(item => item.id === requested.id && item.error) : [...state.messages].reverse().find(item => item.error); return failed?.retryable === true ? send(failed.query, failed.request?.selectedRecordId, failed.request) : Promise.resolve(false); },
    getState: () => state,
    getStateFor: next => sameScope(scope, next) ? state : emptyState(),
    dispose() { disposed = true; active?.abort(); active = null; state = emptyState(); },
  };
}

module.exports = { DEFAULT_ASK, validateCitation, validateAskResponse, createGroundedChatApi, chatErrorMessage, sameScope, createChatSession };
