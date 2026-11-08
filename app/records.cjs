const { requestWithDeadline } = require("./processing.cjs");

const RECORD_SCOPES = new Set(["PERSONAL", "FAMILY", "COMBINED"]);
const VISIBILITIES = new Set(["PRIVATE", "FAMILY", "CUSTOM"]);
const LIFECYCLE_ACTIONS = new Set(["HIDE", "ARCHIVE", "RESTORE", "SOFT_DELETE", "ACTIVATE"]);
const METADATA_FIELDS = new Set([
  "title", "subject_member_ids", "participant_member_ids", "occurred_at", "location", "event_ids", "category",
]);

function idempotencyHeaders(idempotencyKey) {
  if (typeof idempotencyKey !== "string" || !idempotencyKey.trim() || idempotencyKey.length > 200) {
    throw new Error("기록 요청 식별자가 필요합니다.");
  }
  return { "Idempotency-Key": idempotencyKey };
}

function mutationOptions({ recordVersion, reason, idempotencyKey }, json, { post = true } = {}) {
  if (!Number.isSafeInteger(recordVersion) || recordVersion < 1) {
    throw new Error("기록 버전을 확인할 수 없습니다. 기록을 다시 열어주세요.");
  }
  if (typeof reason !== "string" || !reason.trim() || reason.length > 2000) {
    throw new Error("변경 사유를 1~2000자로 입력해주세요.");
  }
  return {
    json: { ...json, reason },
    headers: { "If-Match": `"${recordVersion}"`, ...(post ? idempotencyHeaders(idempotencyKey) : {}) },
  };
}

function createRecordsApi(client, { timeoutMs = 30000 } = {}) {
  const request = (method, path, options = {}) => requestWithDeadline(
    signal => client[method](path, { ...options, signal }), { timeoutMs });
  const recordPath = (recordId, suffix = "") => `/records/${encodeURIComponent(recordId)}${suffix}`;
  return {
    async list(roomId, { scope } = {}) {
      if (scope !== undefined && !RECORD_SCOPES.has(scope)) throw new Error("기록 목록 범위를 확인해주세요.");
      const query = scope === undefined ? "" : `?scope=${scope}`;
      const result = await request("get", `/families/${encodeURIComponent(roomId)}/records${query}`);
      if (!Array.isArray(result?.items)) throw new Error("기록 목록 응답을 확인할 수 없습니다.");
      return result.items;
    },
    get: recordId => request("get", recordPath(recordId)),
    getManagement: recordId => request("get", recordPath(recordId, "/management")),
    create: (payload, idempotencyKey) => request("post", "/records", { json: payload, headers: idempotencyHeaders(idempotencyKey) }),
    approveReview: (recordId, options = {}) => request("post", recordPath(recordId, "/reviews"), mutationOptions(options, {})),
    changeVisibility: (recordId, { visibility, customGrants = [], ...options } = {}) => {
      if (!VISIBILITIES.has(visibility)) throw new Error("공개 범위를 확인해주세요.");
      if (!Array.isArray(customGrants) || customGrants.some(id => typeof id !== "string" || !id.trim())) {
        throw new Error("공유할 가족 구성원을 확인해주세요.");
      }
      if (visibility !== "CUSTOM" && customGrants.length) throw new Error("선택한 가족 공개에서만 구성원을 지정할 수 있습니다.");
      return request("post", recordPath(recordId, "/visibility-changes"), mutationOptions(options, {
        visibility, custom_grants: customGrants,
      }));
    },
    transitionLifecycle: (recordId, { action, ...options } = {}) => {
      if (!LIFECYCLE_ACTIONS.has(action)) throw new Error("기록 상태 변경을 확인해주세요.");
      return request("post", recordPath(recordId, "/lifecycle-transitions"), mutationOptions(options, { action }));
    },
    patchMetadata: (recordId, { changes, ...options } = {}) => {
      if (!changes || typeof changes !== "object" || Array.isArray(changes) || !Object.keys(changes).length) {
        throw new Error("수정할 기록 정보를 입력해주세요.");
      }
      if (Object.keys(changes).some(field => !METADATA_FIELDS.has(field))) {
        throw new Error("현재 수정할 수 없는 기록 정보가 포함되어 있습니다.");
      }
      // The server uses optimistic concurrency for PATCH, without POST's idempotency replay.
      return request("patch", recordPath(recordId, "/metadata"), mutationOptions(options, { changes }, { post: false }));
    },
    async listMetadataRevisions(recordId) {
      const result = await request("get", recordPath(recordId, "/metadata-revisions"));
      if (!Array.isArray(result?.items)) throw new Error("수정 이력 응답을 확인할 수 없습니다.");
      return result.items;
    },
  };
}

async function createTextRecord({ records, uploads, payload, idempotencyKey, assertCurrent = () => {} }) {
  if (payload.type !== "text" || !payload.description?.trim()) throw new Error("저장할 텍스트를 입력해주세요.");
  let record;
  try {
    assertCurrent();
    record = await records.create(payload, idempotencyKey);
    assertCurrent();
    if (!record?.record_id) throw new Error("기록 저장 응답을 확인할 수 없습니다.");
    const published = await uploads.publishText(record.record_id);
    assertCurrent();
    if (published.upload_id !== record.record_id || !published.has_file || !published.file_id) throw new Error("텍스트 원문 저장을 확인할 수 없습니다.");
    // Publishing stores the source; evidence approval/indexing remains a separate human review step.
    return published;
  } catch (error) {
    if (record?.record_id) error.savedRecordId = record.record_id;
    throw error;
  }
}

module.exports = { createRecordsApi, createTextRecord };
