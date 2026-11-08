const PROCESSORS = { text: "text", image: "ocr", voice: "stt" };
const VISIBILITY_LABELS = { PRIVATE: "나만 보기", FAMILY: "가족 전체", CUSTOM: "선택한 가족" };

function mergeRecordDetails(record, detail) {
  if (detail?.record_id !== record.id) throw new Error("기록 응답이 일치하지 않습니다.");
  return { ...record, ...detail, title: detail.title ?? record.title,
    detail: detail.description ?? record.detail, tags: detail.tags ?? record.tags };
}

function recordStatus(record) {
  const lifecycle = { HIDDEN: "숨김", ARCHIVED: "보관됨", SOFT_DELETED: "삭제됨", DELETION_REQUESTED: "삭제 요청됨", PURGED: "삭제 처리됨" };
  if (lifecycle[record.lifecycle_state]) return lifecycle[record.lifecycle_state];
  if (!record.hasFile) return "원본 저장 확인 필요";
  return record.review_status === "APPROVED" ? "기록 승인됨" : "내용 검토 필요";
}

function recordMatchesScope(record, scope) {
  if (scope === "PERSONAL") return record.visibility === "PRIVATE";
  if (scope === "FAMILY") return ["FAMILY", "CUSTOM"].includes(record.visibility);
  return true;
}

function managementError(error) {
  if (error?.status === 412 || error?.status === 428) return "다른 곳에서 기록이 변경되었습니다. 입력 내용은 유지됩니다. 최신 상태를 불러온 뒤 내용을 확인하고 다시 저장해주세요.";
  if (error?.status === 403 || error?.status === 404) return "이 작업의 권한이 없거나 기록에 더 이상 접근할 수 없습니다.";
  if (error?.status === 413) return "서버의 파일 크기 또는 저장 공간 한도를 초과했습니다. 더 작은 파일로 다시 시도해주세요.";
  const failures = { processor_not_configured: "서버에 이 분석 기능이 설정되지 않았습니다. 파일은 저장되어 있습니다.", provider_forbidden: "현재 서버에서 이 분석 기능을 사용할 수 없습니다.", provider_timeout: "분석 응답이 늦습니다. 상태를 확인한 뒤 다시 시도해주세요.", source_changed: "원본이 변경되었습니다. 최신 원본을 불러온 뒤 다시 검토해주세요.", STT_ERROR: "음성 전사에 실패했습니다. 파일 형식과 음성 내용을 확인한 뒤 다시 시도해주세요.", OCR_ERROR: "텍스트 추출에 실패했습니다. 사진을 확인한 뒤 다시 시도해주세요." };
  return failures[error?.code] || error?.message || "요청을 완료하지 못했습니다. 상태를 다시 확인해주세요.";
}

// can_edit_content is owner-only for active records in the current server contract.
// Never compare auth user_id with owner_member_id (different identity namespaces).
function canPrepare(record) {
  return record.capabilities?.can_read === true && record.capabilities?.can_edit_content === true && record.lifecycle_state === "ACTIVE";
}

function createPreparationFlow({ records, uploads, processing, roomId, createKey, assertCurrent = () => {}, onState = () => {} }) {
  let snapshot = null;
  const keys = new Map();
  const actionKey = (name, record) => {
    const key = `${name}:${record.id}:${record.record_version}`;
    if (!keys.has(key)) keys.set(key, createKey());
    return keys.get(key);
  };
  const call = async fn => { assertCurrent(); const result = await fn(); assertCurrent(); return result; };
  const emit = state => { assertCurrent(); onState(state); };
  async function fresh(record) {
    const detail = await call(() => records.get(record.id));
    const next = mergeRecordDetails(record, detail);
    if (next.room_id !== roomId || !canPrepare(next)) throw new Error("이 기록을 검토할 권한이 없습니다.");
    return next;
  }
  function validJob(job, record) {
    if (!job?.job_id || job.record_id !== record.id || job.room_id !== roomId || job.upload_file_id !== record.fileId || job.processor !== PROCESSORS[record.type]) throw new Error("처리 작업의 원본을 확인할 수 없습니다.");
    return job;
  }
  async function observe(record, jobId) {
    const job = validJob(await call(() => processing.get(jobId)), record);
    if (job.status !== "SUCCEEDED") { snapshot = { record, job }; emit(snapshot); return snapshot; }
    const review = await call(() => processing.getReview(job.job_id));
    if (review?.job_id !== job.job_id || review.source_attempt !== job.attempt || !review.source_result_sha256) throw new Error("검토 결과가 최신 원본과 일치하지 않습니다.");
    snapshot = { record, job, review };
    emit(snapshot);
    return snapshot;
  }
  async function start(record, { retry = false } = {}) {
    snapshot = null;
    let current = await fresh(record);
    if (!PROCESSORS[current.type]) throw new Error("이 유형은 현재 검색 준비 화면에서 지원하지 않습니다.");
    if (!current.hasFile && current.type === "text") {
      const source = await call(() => uploads.publishText(current.id));
      if (source?.upload_id !== current.id || !source.has_file || !source.file_id) throw new Error("텍스트 원문 저장을 확인할 수 없습니다.");
      current = await fresh({ ...current, hasFile: true, fileId: source.file_id });
    }
    if (!current.hasFile || !current.fileId) throw new Error("원본 파일 업로드를 먼저 완료해주세요.");
    if (current.review_status !== "APPROVED") {
      current = mergeRecordDetails(current, await call(() => records.approveReview(current.id, {
        recordVersion: current.record_version, reason: "원문을 확인하고 검색 준비를 요청함", idempotencyKey: actionKey("approve", current),
      })));
    }
    let job = validJob(await call(() => processing.create(current.id, { upload_file_id: current.fileId, processor: PROCESSORS[current.type] })), current);
    snapshot = { record: current, job }; emit(snapshot);
    if (job.status === "FAILED" && retry) job = validJob(await call(() => processing.retry(job.job_id)), current);
    if (job.status === "PENDING") {
      emit({ record: current, job, message: "분석 중입니다. 화면을 닫아도 나중에 같은 작업을 확인할 수 있습니다." });
      await call(() => processing.run(job.job_id, { timeoutMs: 120000 }));
    }
    return observe(current, job.job_id);
  }
  async function decide(action) {
    if (!["approve", "reject", "index"].includes(action) || !snapshot?.review) throw new Error("먼저 최신 분석 결과를 확인해주세요.");
    const displayed = snapshot;
    const record = await fresh(displayed.record);
    const current = await observe(record, displayed.job.job_id);
    if (!current.review || current.review.source_attempt !== displayed.review.source_attempt || current.review.source_result_sha256 !== displayed.review.source_result_sha256) throw new Error("분석 결과가 변경되었습니다. 새 결과를 확인하고 다시 결정해주세요.");
    let review = current.review;
    if (action !== "index" && review.review_status === "unreviewed") {
      await call(() => processing.review(current.job.job_id, action));
      review = (await observe(record, current.job.job_id)).review;
    }
    if (!review || review.source_attempt !== displayed.review.source_attempt || review.source_result_sha256 !== displayed.review.source_result_sha256) throw new Error("분석 결과가 변경되었습니다. 새 결과를 확인하고 다시 결정해주세요.");
    if (action === "reject") {
      if (review.review_status !== "rejected") throw new Error("이미 승인된 결과는 거절로 바꿀 수 없습니다.");
      return snapshot;
    }
    if (review.review_status !== "confirmed" || review.user_confirmed !== true) throw new Error("분석 결과 승인 여부를 확인해주세요.");
    if (record.review_status !== "APPROVED") throw new Error("기록 내용이 변경되었습니다. 원문을 다시 확인해주세요.");
    emit({ ...snapshot, message: "검색 준비 중입니다." });
    const indexed = await call(() => processing.index(record.id, current.job.job_id));
    if (indexed?.source_attempt !== review.source_attempt || indexed?.source_result_sha256 !== review.source_result_sha256) throw new Error("검색 결과의 원본이 변경되었습니다. 최신 결과를 다시 확인해주세요.");
    if (!(Number.isInteger(indexed?.chunk_count) && indexed.chunk_count > 0 && Number.isInteger(indexed?.embedding_count) && indexed.embedding_count > 0)) throw new Error("검색 준비 완료를 확인하지 못했습니다. 같은 기록으로 다시 시도해주세요.");
    snapshot = { ...snapshot, indexed: true, message: "검색·대화에 사용할 준비가 완료되었습니다." };
    emit(snapshot);
    return snapshot;
  }
  return { start, decide, actionKey };
}

module.exports = { PROCESSORS, VISIBILITY_LABELS, mergeRecordDetails, recordStatus, recordMatchesScope, managementError, canPrepare, createPreparationFlow };
