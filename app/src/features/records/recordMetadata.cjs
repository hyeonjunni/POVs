const FIELD_LABELS = {
  title: "제목", occurred_at: "발생일", location: "장소", category: "분류",
  subject_member_ids: "기록 속 가족", participant_member_ids: "참여한 가족", event_ids: "연결된 사건",
};

function metadataDraft(record) {
  const metadata = record?.metadata || {};
  return {
    title: String(record?.title ?? ""),
    occurred_at: String(metadata.occurred_at ?? ""),
    location: String(metadata.location ?? ""),
    category: String(metadata.category ?? ""),
  };
}

function validCalendarDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  if (year < 1 || month < 1 || month > 12 || day < 1) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= days[month - 1];
}

function metadataChanges(record, draft) {
  if (record?.capabilities?.can_edit_metadata !== true) throw new Error("기록 정보를 수정할 권한이 없습니다.");
  const before = metadataDraft(record);
  const changes = {};
  if (record.capabilities.can_edit_content === true && draft.title !== before.title) {
    const title = String(draft.title ?? "").trim();
    if (!title) throw new Error("제목을 입력해주세요.");
    if (title !== before.title) changes.title = title;
  }
  for (const field of ["occurred_at", "location", "category"]) {
    if (draft[field] === before[field]) continue;
    const value = String(draft[field] ?? "").trim();
    if (value === before[field]) continue;
    if (field === "occurred_at" && value && !validCalendarDate(value)) {
      throw new Error("발생일은 실제 날짜를 YYYY-MM-DD 형식으로 입력해주세요.");
    }
    changes[field] = value || null;
  }
  return changes;
}

function revisionSummary(revision) {
  const fields = Array.isArray(revision?.changed_fields) ? revision.changed_fields : [];
  return fields.map(field => FIELD_LABELS[field] || "기록 정보").filter((value, index, all) => all.indexOf(value) === index).join(" · ") || "기록 정보";
}

module.exports = { metadataDraft, metadataChanges, validCalendarDate, revisionSummary };
