export const uploadTypes = [{
  key: "voice",
  label: "음성",
  icon: "VO",
  color: "#2563EB",
  tint: "#DBEAFE",
  description: "대화, 조언, 레시피, 가족 인터뷰 음성 기록을 저장합니다."
}, {
  key: "text",
  label: "텍스트",
  icon: "TX",
  color: "#0F766E",
  tint: "#CCFBF1",
  description: "메모, 편지, 일정, 생활 정보 같은 텍스트 기록을 남깁니다."
}, {
  key: "image",
  label: "이미지",
  icon: "IMG",
  color: "#EA580C",
  tint: "#FFEDD5",
  description: "가족 사진, 문서 이미지, 장소 기록을 디바이스에서 업로드합니다."
}, {
  key: "video",
  label: "영상",
  icon: "VID",
  color: "#7C3AED",
  tint: "#EDE9FE",
  description: "인터뷰, 행사 영상, 일상 장면을 디바이스에서 업로드합니다."
}];
export const initialItems = [];
export function parseTagInput(value) {
  return Array.from(new Set(String(value || "").split(",").map(item => item.trim()).filter(Boolean)));
}
export function buildDefaultTagsForType(typeKey) {
  if (typeKey === "image") {
    return ["이미지", "가족기록"];
  }
  if (typeKey === "video") {
    return ["영상", "가족기록"];
  }
  if (typeKey === "voice") {
    return ["음성", "가족기록"];
  }
  return ["텍스트", "가족기록"];
}
export function formatRecordDate(value) {
  if (!value) {
    const now = new Date();
    return `${now.getFullYear()}.${String(now.getMonth() + 1).padStart(2, "0")}.${String(now.getDate()).padStart(2, "0")}`;
  }
  if (/^\d{4}-\d{2}(-\d{2})?$/.test(value)) return value.replace(/-/g, ".");
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)) return String(value);
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return `${parsed.getFullYear()}.${String(parsed.getMonth() + 1).padStart(2, "0")}.${String(parsed.getDate()).padStart(2, "0")}`;
}
export function mapUploadToRecord(upload) {
  const occurredAt = upload.metadata?.occurred_at;
  const authoredAt = upload.metadata?.authored_at;
  return {
    ...upload,
    id: upload.upload_id,
    type: upload.type,
    displayType: upload.metadata?.record_kind === "EVENT" ? "event" : upload.type,
    title: upload.title,
    detail: upload.description || "추가 설명 없음",
    tags: Array.isArray(upload.tags) ? upload.tags : [],
    createdAt: formatRecordDate(upload.created_at),
    displayDate: `${occurredAt ? "발생일" : authoredAt ? "작성일" : "등록일"} ${formatRecordDate(occurredAt || authoredAt || upload.created_at)}`,
    fileUrl: upload.file_url || null,
    fileId: upload.file_id || null,
    mimeType: upload.mime_type || null,
    hasFile: Boolean(upload.has_file)
  };
}
