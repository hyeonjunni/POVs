const { test } = require("node:test");
const assert = require("node:assert/strict");
const { metadataDraft, metadataChanges, validCalendarDate, revisionSummary } = require("./src/features/records/recordMetadata.cjs");
const record = { title: "가족 여행", capabilities: { can_edit_metadata: true, can_edit_content: true }, metadata: { occurred_at: "2024-02-29T13:30:00+09:00", location: "강릉", category: "여행" } };

test("an untouched timestamp is preserved exactly when editing another field", () => {
  const draft = { ...metadataDraft(record), location: "서울" };
  assert.equal(draft.occurred_at, "2024-02-29T13:30:00+09:00");
  assert.deepEqual(metadataChanges(record, draft), { location: "서울" });
  assert.deepEqual(metadataChanges(record, metadataDraft(record)), {});
});

test("date changes validate the calendar, including leap years, without local timezone conversion", () => {
  for (const value of ["2024-02-29", "2000-02-29", "0001-01-01", "2026-12-31"]) assert.equal(validCalendarDate(value), true, value);
  for (const value of ["2026-02-29", "1900-02-29", "2026-04-31", "2026-00-01", "0000-01-01", "2026-1-01", "2026-01-01T00:00:00Z"]) {
    assert.equal(validCalendarDate(value), false, value);
    assert.throws(() => metadataChanges(record, { ...metadataDraft(record), occurred_at: value }), /실제 날짜/);
  }
  assert.deepEqual(metadataChanges(record, { ...metadataDraft(record), occurred_at: "2026-10-11" }), { occurred_at: "2026-10-11" });
});

test("optional metadata can be deliberately cleared but empty or absent values are not sent unless changed", () => {
  assert.deepEqual(metadataChanges(record, { ...metadataDraft(record), location: " ", category: "", occurred_at: "" }), { occurred_at: null, location: null, category: null });
  const empty = { ...record, metadata: {} };
  assert.deepEqual(metadataChanges(empty, metadataDraft(empty)), {});
  assert.deepEqual(metadataChanges(record, { ...metadataDraft(record), location: " 강릉 " }), {});
});

test("only owner-editable title and permitted metadata fields are emitted", () => {
  assert.throws(() => metadataChanges(record, { ...metadataDraft(record), title: " " }), /제목/);
  const nonOwner = { ...record, capabilities: { can_edit_metadata: true, can_edit_content: false } };
  assert.deepEqual(metadataChanges(nonOwner, { ...metadataDraft(record), title: "바꿀 수 없음", location: "부산", description: "본문", tags: ["태그"], participant_member_ids: ["id"] }), { location: "부산" });
  assert.throws(() => metadataChanges({ ...record, capabilities: { can_edit_metadata: false } }, metadataDraft(record)), /권한/);
});

test("building a change does not mutate the saved baseline or a draft that must survive failure", () => {
  const baseline = structuredClone(record);
  const draft = { ...metadataDraft(record), category: "모임" };
  assert.deepEqual(metadataChanges(record, draft), { category: "모임" });
  assert.deepEqual(record, baseline);
  assert.equal(draft.category, "모임");
  assert.equal(record.metadata.category, "여행");
});

test("revision summaries use readable labels and never expose unknown IDs or field names", () => {
  assert.equal(revisionSummary({ changed_fields: ["title", "subject_member_ids", "private_object_uuid"] }), "제목 · 기록 속 가족 · 기록 정보");
  assert.equal(revisionSummary({ changed_fields: [] }), "기록 정보");
});
