const test = require("node:test");
const assert = require("node:assert/strict");
const { temporal, presentChatResults } = require("./src/features/chat/chatResultPresentation.cjs");

const date = (precision, value, granularity = precision) => ({ precision, value, granularity, timezone: "Asia/Seoul" });
const sources = [{ entity_id: "event-1", record_id: "record-1", record_version: 3, source_revision: 2 }];
const citation = { record_id: "record-1", claim_id: "claim-1", claim_version: 2 };
const entry = () => ({ event_id: "event-1", title: "가족 여행", event_type: "TRIP", event_status: "COMPLETED", occurred_at: date("SEASON", "2025-SUMMER"), location: { label: "강릉" }, member_modes: { "member-1": ["ONSITE"], "user-1": ["REMOTE"] }, sources });
const structuredResponse = changes => ({ gate_route: "ANSWER", citations: [citation], answer: "서버의 원문 답변", structured_result: { route: "ANSWER", plan: { operation: "TIMELINE" }, entries: [entry()], member_labels: { "member-1": "보라" }, ...changes } });
const perspective = changes => ({ claim_id: "claim-1", claim_version: 2, claimant_member_id: "member-2", subject_member_id: "member-1", source_narrator_member_id: "member-3", epistemic_basis: "RETROSPECTIVE_MEMORY", perspective_type: "RECOLLECTION", proposition: { subject_id: "member-1", predicate: "MEMORY_STATE", value: "DOES_NOT_REMEMBER", polarity: "AFFIRMED" }, event_occurred_at: date("YEAR", "2006"), recorded_at: date("EXACT", "2026-10-11", "DAY"), citation_index: 0, ...changes });
const claimResponse = changes => ({ gate_route: "ANSWER", citations: [citation], claim_result: { operation: "PARTICIPATION_MEMORY_COMPARE", perspectives: [perspective()], participation: [], relations: [], ...changes } });

test("temporal precision preserves year/month/season without invented dates", () => {
  assert.deepEqual(temporal(date("YEAR", "2006")), { text: "2006년", precision: "연도 단위", known: true });
  assert.equal(temporal(date("MONTH", "2006-04")).text, "2006년 4월");
  assert.equal(temporal(date("SEASON", "2006-SPRING")).text, "2006년 봄");
  assert.equal(temporal(date("EXACT", "2006-04-01", "DAY")).text, "2006년 4월 1일");
  assert.equal(temporal(date("EXACT", "2026-10-11T00:30:00+09:00", "INSTANT")).text, "2026-10-11 00:30:00+09:00");
});

test("unknown, unsupported and invalid dates remain unknown", () => {
  for (const value of [null, [], {}, { precision: "UNKNOWN", value: "2025-01-01", granularity: "DAY" }, date("MONTH", "2025-01-01", "DAY"), date("EXACT", "2025-02-29", "DAY"), date("EXACT", "0000-01-01", "DAY"), date("EXACT", "2026-10-11T24:00:00+09:00", "INSTANT"), date("YEAR", "unknown"), date("constructor", "2025"), { precision: "APPROXIMATE", original_text: "어릴 때" }, { precision: "RANGE", granularity: "DAY", start: "2025-12-30", end: "2025-01-01" }]) {
    assert.deepEqual(temporal(value), { text: "시점 미확인", precision: "미확인", known: false });
  }
  assert.equal(temporal(date("EXACT", "2024-02-29", "DAY")).known, true);
});

test("approximation retains original wording and qualification; ranges retain bounds", () => {
  assert.equal(temporal({ precision: "APPROXIMATE", original_text: "초등학교 무렵", qualification: "정확한 연도 미상" }).text, "초등학교 무렵 (정확한 연도 미상)");
  assert.equal(temporal({ precision: "RANGE", start: "2020-01", end: "2020-03", granularity: "MONTH" }).text, "2020년 1월 ~ 2020년 3월");
  assert.equal(temporal({ precision: "RANGE", start: "2026-01-01T23:30:00+09:00", end: "2026-01-01T16:00:00Z", granularity: "INSTANT" }).known, true);
});

test("timeline binds only existing cited records and uses server member labels without identity guessing", () => {
  const input = structuredResponse({ entries: [entry(), { ...entry(), title: "연결되지 않은 사건", sources: [{ record_id: "not-cited" }] }, null] });
  const original = JSON.stringify(input);
  const result = presentChatResults(input).structured;
  assert.equal(result.title, "기록 속 타임라인");
  assert.equal(result.entries.length, 1);
  assert.equal(result.entries[0].when.text, "2025년 여름");
  assert.deepEqual(result.entries[0].participants, ["보라 · 현장 참여"]);
  assert.deepEqual(result.entries[0].citationIndices, [0]);
  assert.equal(JSON.stringify(input), original);
  assert.doesNotMatch(JSON.stringify(result), /user-1|member-1/);
});

test("counts use server totals including zero, never visible row count or strings", () => {
  const result = presentChatResults(structuredResponse({ plan: { operation: "COUNT" }, count: 14, grouped_counts: { "member-1": 0, "user-1": 9, bad: -1 } })).structured;
  assert.equal(result.count, 14);
  assert.deepEqual(result.groups, [{ key: "member-1", label: "보라", count: 0 }]);
  assert.equal(presentChatResults(structuredResponse({ count: 0 })).structured.count, 0);
  for (const count of [undefined, -1, 0.5, "14", NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) assert.equal(presentChatResults(structuredResponse({ count })).structured.count, null);
});

test("gift first use remains unknown even when a later use is confirmed", () => {
  const result = presentChatResults(structuredResponse({ plan: { compare_gift_use: true }, received_at: date("MONTH", "2024-03"), first_used_at: null, use_confirmed_at: date("EXACT", "2024-05-01", "DAY"), reason: "FIRST_USE_NOT_RECORDED" })).structured;
  assert.equal(result.milestones[0].when.text, "2024년 3월");
  assert.equal(result.milestones[1].when.known, false);
  assert.equal(result.milestones[2].when.text, "2024년 5월 1일");
  assert.match(result.notice, /최초 사용일은 기록에서 확인되지/);
});

test("actual route/reason produces clarification and invented NEEDS_DATA fields do not", () => {
  const result = presentChatResults({ ...structuredResponse({ route: "CLARIFY", reason: "OVERLAPPING_TIMES" }), gate_route: "CLARIFY", citations: [] }).structured;
  assert.match(result.notice, /날짜 범위가 겹쳐/);
  assert.deepEqual(result.entries, []);
  assert.equal(presentChatResults(structuredResponse({ status: "NEEDS_DATA" })).structured.notice, null);
  for (const gate_route of ["REFUSE", "unsupported", "NEEDS_DATA"]) assert.deepEqual(presentChatResults({ ...structuredResponse(), gate_route }), { structured: null, claims: null });
  assert.equal(presentChatResults({ ...structuredResponse(), gate_route: "NO_RECORD", citations: [] }).structured, null);
});

test("cited gift NEEDS_DATA preserves known milestones without inventing first use", () => {
  const input = { ...structuredResponse({ plan: { compare_gift_use: true }, received_at: date("MONTH", "2024-03"), first_used_at: null, use_confirmed_at: date("EXACT", "2024-05-01", "DAY"), reason: "FIRST_USE_NOT_RECORDED" }), gate_route: "NEEDS_DATA", claim_result: claimResponse().claim_result };
  const result = presentChatResults(input);
  assert.equal(result.structured.milestones[0].when.text, "2024년 3월");
  assert.equal(result.structured.milestones[1].when.known, false);
  assert.equal(result.structured.milestones[2].when.text, "2024년 5월 1일");
  assert.deepEqual(result.structured.entries[0].citationIndices, [0]);
  assert.equal(result.claims, null);
  for (const change of [{ route: "CLARIFY" }, { reason: "OTHER" }, { plan: {} }, { first_used_at: date("EXACT", "2024-03-01", "DAY") }, { entries: [] }, { entries: [{ ...entry(), sources: [{ record_id: "other" }] }] }]) {
    assert.deepEqual(presentChatResults({ ...input, structured_result: { ...input.structured_result, ...change } }), { structured: null, claims: null });
  }
  for (const citations of [[], [{}], [{ record_id: "other" }]]) assert.deepEqual(presentChatResults({ ...input, citations }), { structured: null, claims: null });
});

test("claim memory is explicit, source bound, and separate from participation", () => {
  const result = presentChatResults(claimResponse({ participation: [{ member_id: "member-1", member_label: "보라", mode: "ONSITE", occurred_at: null, sources, citation_indices: [0, 0] }] })).claims;
  assert.equal(result.perspectives[0].title, "근거 1의 관점");
  assert.equal(result.perspectives[0].memory, "기억하지 못한다고 명시됨");
  assert.equal(result.perspectives[0].basis, "회상");
  assert.deepEqual(result.participation[0].citationIndices, [0]);
  assert.equal(result.participation[0].label, "보라");
  assert.equal(result.participation[0].mode, "현장 참여");
  assert.match(result.notices[0], /참여 여부와 기억 상태는 별개/);
  const participationOnly = presentChatResults(claimResponse({ perspectives: [], participation: [{ member_label: "보라", mode: "MENTIONED", sources, citation_indices: [0] }] })).claims;
  assert.deepEqual(participationOnly.perspectives, []);
  assert.equal(participationOnly.participation[0].mode, "기록에 언급");
});

test("self-reported memory state is not labeled as direct experience of the event", () => {
  const self = perspective({ claimant_member_id: "member-1", source_narrator_member_id: "member-1", epistemic_basis: "DIRECT_EXPERIENCE", perspective_type: "DIRECT_MEMORY" });
  const card = value => presentChatResults(claimResponse({ perspectives: [value] })).claims.perspectives[0];
  for (const state of ["REMEMBERS", "FAINT", "DOES_NOT_REMEMBER", "HEARD_FROM_OTHER", "UNKNOWN"]) {
    const result = card({ ...self, proposition: { ...self.proposition, value: state } });
    assert.equal(result.basis, "본인 기억 상태");
    assert.ok(result.memory);
    assert.deepEqual(result.citationIndices, [0]);
  }
  for (const changes of [
    { claimant_member_id: "member-2" }, { source_narrator_member_id: "member-2" },
    { proposition: { ...self.proposition, polarity: "NEGATED" } },
    { proposition: { ...self.proposition, predicate: "ACTION" } },
  ]) assert.equal(card({ ...self, ...changes }).basis, "직접 경험");
  assert.equal(card({ ...self, epistemic_basis: "HEARD_FROM_OTHER" }).basis, "전해 들은 내용");
  assert.equal(card({ ...self, epistemic_basis: "INTERPRETATION" }).basis, "해석");
  assert.equal(card({ ...self, epistemic_basis: "RETROSPECTIVE_MEMORY" }).basis, "회상");
});

test("malformed claim indexes, versions, labels, and identity mismatches do not create claims or names", () => {
  for (const changes of [{ citation_index: -1 }, { citation_index: 1 }, { citation_index: "0" }, { claim_id: "wrong-claim" }, { claim_version: 3 }, { claim_version: "2" }]) {
    assert.equal(presentChatResults(claimResponse({ operation: "PERSPECTIVE_COMPARE", perspectives: [perspective(changes)] })).claims, null);
  }
  for (const changes of [{ subject_member_id: "not-the-proposition-subject" }, { proposition: { predicate: "MEMORY_STATE", value: "REMEMBERS", polarity: "NEGATED", subject_id: "member-1" } }]) assert.equal(presentChatResults(claimResponse({ perspectives: [perspective(changes)] })).claims.perspectives[0].memory, null);
  for (const row of [{ member_id: "member-1", mode: "ONSITE", sources, citation_indices: [0] }, { member_label: "보라", mode: "UNKNOWN", sources, citation_indices: [0] }, { member_label: "보라", mode: "ONSITE", sources: [{ record_id: "wrong-record" }], citation_indices: [0] }]) assert.deepEqual(presentChatResults(claimResponse({ participation: [row] })).claims.participation, []);
});

test("unanswerable emotion is shown as unknown, never inferred from memory or participation", () => {
  const result = presentChatResults(claimResponse({ operation: "MENTAL_STATE_QUERY", emotion_answerable: false, emotion_supported: false })).claims;
  assert.match(result.notices.join(" "), /감정 판단 근거 부족/);
  const known = presentChatResults(claimResponse({ operation: "MENTAL_STATE_QUERY", emotion_answerable: true, emotion_supported: false })).claims;
  assert.equal(known.notices.length, 0);
  assert.doesNotMatch(JSON.stringify(known), /행복|슬픔|두려움/);
});

test("different perspectives are not called contradictions without a bound server relation", () => {
  const input = claimResponse({ operation: "PERSPECTIVE_COMPARE", relations: [{ left: "claim-1", right: "claim-2", relation: "CONTRADICTS" }] });
  assert.deepEqual(presentChatResults(input).claims.notices, []);
  input.citations.push({ record_id: "record-2", claim_id: "claim-2", claim_version: 1 });
  input.claim_result.perspectives.push(perspective({ claim_id: "claim-2", claim_version: 1, citation_index: 1 }));
  assert.match(presentChatResults(input).claims.notices[0], /서로 다른 주장/);
  input.claim_result.relations[0].relation = "UNRESOLVED";
  assert.match(presentChatResults(input).claims.notices[0], /아직 확정되지/);
  input.claim_result.relations[0].relation = "COMPATIBLE";
  assert.deepEqual(presentChatResults(input).claims.notices, []);
});

test("unexpected optional response shapes do not crash or become user-facing raw objects", () => {
  for (const input of [null, undefined, [], 42, {}, { gate_route: "ANSWER", citations: {}, structured_result: [], claim_result: "invalid" }, { gate_route: "ANSWER", citations: [], structured_result: { route: "ANSWER" }, claim_result: {} }]) {
    assert.deepEqual(presentChatResults(input), { structured: null, claims: null });
  }
});
