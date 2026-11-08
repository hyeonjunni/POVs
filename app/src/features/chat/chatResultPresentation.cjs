// Presentation only: never resolve identities, recalculate counts, or manufacture
// source URLs. Original opening continues through the validated Ask citations.
const object = value => value != null && typeof value === "object" && !Array.isArray(value);
const text = value => typeof value === "string" && value.trim() ? value.trim() : null;
const list = value => Array.isArray(value) ? value : [];
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
const own = (value, key) => object(value) && Object.prototype.hasOwnProperty.call(value, key) ? value[key] : undefined;
const modes = { ONSITE: "현장 참여", REMOTE: "원격 참여", SUPPORT: "지원", MENTIONED: "기록에 언급", NOT_PRESENT: "불참 기록" };
const eventTypes = { TRIP: "여행", BIRTHDAY: "생일", HEALTH_EXAM: "건강 검진", HEALTH_REPORT: "검진 결과", DIAGNOSIS: "진단", EDUCATION: "교육", MILITARY: "군 복무", FAMILY_ACTIVITY: "가족 활동", WORK: "직장", OTHER: "기타" };
const eventStates = { PLANNED: "예정", ONGOING: "진행 중", COMPLETED: "완료", CANCELLED: "취소", UNKNOWN: "상태 미확인" };
const bases = { DIRECT_EXPERIENCE: "직접 경험", RETROSPECTIVE_MEMORY: "회상", HEARD_FROM_OTHER: "전해 들은 내용", DOCUMENTED_SOURCE: "문서 기록", INTERPRETATION: "해석", UNKNOWN: "근거 유형 미확인" };
const memories = { REMEMBERS: "기억함", FAINT: "희미하게 기억함", DOES_NOT_REMEMBER: "기억하지 못한다고 명시됨", HEARD_FROM_OTHER: "전해 들은 기억", UNKNOWN: "기억 상태 미확인" };
const reasons = {
  FIRST_USE_NOT_RECORDED: "최초 사용일은 기록에서 확인되지 않았습니다. 사용 확인일과 구분해서 보세요.",
  TEMPORAL_UNCERTAINTY: "날짜가 충분히 명확하지 않습니다. 시점이나 기간을 구체적으로 알려 주세요.",
  OVERLAPPING_TIMES: "기록의 날짜 범위가 겹쳐 순서를 확정할 수 없습니다.",
  RESULT_LIMIT_REFINE_QUERY: "결과가 많습니다. 기간이나 대상을 좁혀서 질문해 주세요.",
  MEMBER_SET_AMBIGUOUS: "누구에 대한 질문인지 구체적으로 알려 주세요.",
  GIFT_IDENTITY_AMBIGUOUS: "어떤 선물인지 구체적으로 알려 주세요.",
  GIFT_IDENTITY_OR_TIME_AMBIGUOUS: "선물이나 사용 시점을 구분할 근거가 더 필요합니다.",
  GIFT_RECEIPT_ROOT_REQUIRED: "어떤 선물을 받은 기록인지 구체적으로 알려 주세요.",
  GIFT_SUBJECT_REQUIRED: "누가 받은 선물인지 구체적으로 알려 주세요.",
  SERVICE_INTERVAL_REQUIRED: "비교할 군 복무 기간을 확인할 기록이 필요합니다.",
  TIME_WINDOW_UNSUPPORTED: "비교할 기간을 구체적인 날짜로 알려 주세요.",
  UNSUPPORTED_GRAMMAR: "대상과 기간을 나누어 조금 더 간단하게 질문해 주세요.",
  EXPLICIT_EVENT_TYPE_BINDING_REQUIRED: "사건의 종류를 확인할 근거가 더 필요합니다.",
  NO_AUTHORIZED_MATCH: "현재 열람 가능한 기록에서 조건에 맞는 사건을 찾지 못했습니다.",
  NO_AUTHORIZED_PARENT: "비교의 기준이 되는 기록을 확인할 수 없습니다.",
  NO_AUTHORIZED_INTERVAL: "비교할 기간의 기록을 확인할 수 없습니다.",
  NO_AUTHORIZED_INTERVAL_SUBJECT: "해당 인물의 기간 기록을 확인할 수 없습니다.",
};

function calendarValue(value, granularity) {
  if (!text(value)) return null;
  if (granularity === "YEAR" && /^[0-9]{4}$/.test(value) && Number(value) > 0) return `${value}년`;
  if (granularity === "MONTH" && /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(value) && Number(value.slice(0, 4)) > 0) return `${value.slice(0, 4)}년 ${Number(value.slice(5))}월`;
  if (granularity === "SEASON") {
    const match = /^([0-9]{4})-(SPRING|SUMMER|AUTUMN|WINTER)$/.exec(value);
    if (match && Number(match[1]) > 0) return `${match[1]}년 ${{ SPRING: "봄", SUMMER: "여름", AUTUMN: "가을", WINTER: "겨울" }[match[2]]}`;
  }
  if (granularity === "DAY") {
    const match = /^([0-9]{4})-(0[1-9]|1[0-2])-([0-9]{2})$/.exec(value);
    if (!match || Number(match[1]) < 1) return null;
    const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    if (day < 1 || day > [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]) return null;
    return `${year}년 ${month}월 ${day}일`;
  }
  // Keep the source offset; do not shift a day in the device's timezone.
  if (granularity === "INSTANT" && /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$/.test(value) &&
      calendarValue(value.slice(0, 10), "DAY") && Number(value.slice(11, 13)) < 24 && Number(value.slice(14, 16)) < 60 && Number(value.slice(17, 19)) < 60 &&
      Number.isFinite(Date.parse(value))) return value.replace("T", " ").replace(/Z$/, " UTC");
  return null;
}

function temporal(value) {
  const unknown = { text: "시점 미확인", precision: "미확인", known: false };
  if (!object(value) || value.precision === "UNKNOWN") return unknown;
  if (value.precision === "APPROXIMATE") {
    const original = text(value.original_text), qualification = text(value.qualification);
    return original && qualification ? { text: `${original} (${qualification})`, precision: "대략적인 시점", known: true } : unknown;
  }
  if (value.precision === "RANGE") {
    if (value.granularity === "SEASON") return unknown;
    const start = calendarValue(value.start, value.granularity), end = calendarValue(value.end, value.granularity);
    const ordered = value.granularity === "INSTANT" ? Date.parse(value.start) <= Date.parse(value.end) : value.start <= value.end;
    return start && end && ordered ? { text: `${start} ~ ${end}`, precision: "기간", known: true } : unknown;
  }
  const compatible = { EXACT: ["DAY", "INSTANT"], MONTH: ["MONTH"], YEAR: ["YEAR"], SEASON: ["SEASON"] };
  if (!own(compatible, value.precision)?.includes(value.granularity)) return unknown;
  const formatted = calendarValue(value.value, value.granularity);
  return formatted ? { text: formatted, precision: { EXACT: "날짜 확인", MONTH: "월 단위", YEAR: "연도 단위", SEASON: "계절 단위" }[value.precision], known: true } : unknown;
}

function sourceIndices(sources, citations) {
  const recordIds = new Set(list(sources).filter(object).map(source => text(source.record_id)).filter(Boolean));
  return citations.flatMap((citation, index) => recordIds.has(text(citation?.record_id)) ? [index] : []);
}

function structured(result, citations, canShowFacts) {
  if (!object(result) || !["ANSWER", "NO_RECORD", "CLARIFY", "UNSUPPORTED"].includes(result.route)) return null;
  const notice = text(own(reasons, result.reason));
  if (result.route !== "ANSWER") return notice ? { notice, entries: [], groups: [], milestones: [], count: null } : null;
  if (!canShowFacts || !citations.length) return null;
  const labels = object(result.member_labels) ? result.member_labels : {};
  const entries = list(result.entries).slice(0, 500).filter(object).flatMap((entry, index) => {
    const title = text(entry.title), indices = sourceIndices(entry.sources, citations);
    if (!title || !indices.length) return [];
    return [{ key: `${text(entry.event_id) || "event"}:${index}`, title, when: temporal(entry.occurred_at),
      type: text(own(eventTypes, entry.event_type)), state: text(own(eventStates, entry.event_status)) || "상태 미확인",
      location: object(entry.location) ? text(entry.location.label) : null,
      participants: object(entry.member_modes) ? Object.entries(entry.member_modes).flatMap(([id, values]) => {
        const label = text(own(labels, id));
        const names = [...new Set(list(values).map(mode => text(own(modes, mode))).filter(Boolean))];
        return label && names.length ? [`${label} · ${names.join(", ")}`] : [];
      }) : [], citationIndices: indices }];
  });
  const groups = object(result.grouped_counts) ? Object.entries(result.grouped_counts).flatMap(([id, value]) => {
    const label = text(own(labels, id));
    return count(value) !== null && label ? [{ key: id, label, count: value }] : [];
  }) : [];
  const hasGift = result.plan?.compare_gift_use === true || result.received_at != null || result.first_used_at != null || result.use_confirmed_at != null || result.reason === "FIRST_USE_NOT_RECORDED";
  const milestones = hasGift ? [["받은 시점", result.received_at], ["최초 사용 시점", result.first_used_at], ["사용 확인 시점", result.use_confirmed_at]].map(([label, when]) => ({ label, when: temporal(when) })) : [];
  return { notice, title: result.plan?.operation === "COUNT" ? "기록에서 확인한 횟수" : result.plan?.operation === "TIMELINE" ? "기록 속 타임라인" : "조건에 맞는 기록", count: count(result.count), groups, entries, milestones };
}

function claims(result, citations, canShowFacts) {
  if (!object(result) || !canShowFacts || !citations.length) return null;
  const perspectives = list(result.perspectives).slice(0, 100).filter(object).flatMap((value, index) => {
    const citation = Number.isSafeInteger(value.citation_index) ? citations[value.citation_index] : null;
    // A claim card must refer to the same immutable claim as its existing source.
    if (!citation || !text(value.claim_id) || citation.claim_id !== value.claim_id ||
        !Number.isSafeInteger(value.claim_version) || value.claim_version < 1 || citation.claim_version !== value.claim_version) return [];
    const proposition = object(value.proposition) ? value.proposition : {};
    const memory = proposition.predicate === "MEMORY_STATE" && proposition.polarity === "AFFIRMED" && text(value.subject_member_id) &&
      value.subject_member_id === proposition.subject_id ? text(own(memories, proposition.value)) : null;
    const selfMemory = memory && value.epistemic_basis === "DIRECT_EXPERIENCE" &&
      value.claimant_member_id === value.subject_member_id && value.source_narrator_member_id === value.subject_member_id;
    return [{ key: `${value.claim_id}:${value.claim_version}:${index}`, claimId: value.claim_id,
      title: `근거 ${value.citation_index + 1}의 관점`, basis: selfMemory ? "본인 기억 상태" : text(own(bases, value.epistemic_basis)) || "근거 유형 미확인", memory,
      dates: [["사건 시점", value.event_occurred_at], ["기록 시점", value.recorded_at], ["회상 출처 시점", value.recollection_source_date]]
        .filter(([, date]) => date != null).map(([label, date]) => ({ label, when: temporal(date) })), citationIndices: [value.citation_index] }];
  });
  const participation = list(result.participation).slice(0, 500).filter(object).flatMap((value, index) => {
    const label = text(value.member_label), mode = text(own(modes, value.mode));
    const sourceMatches = new Set(sourceIndices(value.sources, citations));
    const indices = [...new Set(list(value.citation_indices).filter(value => Number.isSafeInteger(value) && value >= 0 && value < citations.length && sourceMatches.has(value)))];
    if (!label || !mode || !indices.length) return [];
    return [{ key: `participation:${index}`, label, mode, when: value.occurred_at != null ? temporal(value.occurred_at) : null, citationIndices: indices }];
  });
  const notices = [];
  if (result.operation === "PARTICIPATION_MEMORY_COMPARE") notices.push("참여 여부와 기억 상태는 별개입니다. 명시된 기억 기록이 없으면 기억 여부를 판단하지 않습니다.");
  if (result.operation === "MENTAL_STATE_QUERY" && result.emotion_answerable === false) notices.push("감정 판단 근거 부족: 승인된 기록으로 해당 감정을 확정할 수 없습니다. 행동이나 기억 부재로 감정을 추정하지 않습니다.");
  const ids = new Set(perspectives.map(value => value.claimId));
  const relations = list(result.relations).filter(value => object(value) && ids.has(value.left) && ids.has(value.right));
  if (relations.some(value => value.relation === "CONTRADICTS")) notices.push("기록에 서로 다른 주장이 있습니다. 어느 쪽이 맞는지는 원본 근거와 함께 확인하세요.");
  else if (relations.some(value => value.relation === "UNRESOLVED")) notices.push("관점 사이의 관계는 아직 확정되지 않았습니다.");
  return perspectives.length || participation.length || notices.length ? { perspectives, participation, notices } : null;
}

function presentChatResults(response) {
  if (object(response) && response.gate_route === "NEEDS_DATA") {
    const result = response.structured_result;
    const gift = object(result) && result.route === "ANSWER" && result.reason === "FIRST_USE_NOT_RECORDED" &&
      result.plan?.compare_gift_use === true && result.first_used_at === null;
    const details = gift ? structured(result, list(response.citations), true) : null;
    return { structured: details?.entries.length ? details : null, claims: null };
  }
  if (!object(response) || !["ANSWER", "CONFLICT", "CLARIFY", "NO_RECORD"].includes(response.gate_route)) return { structured: null, claims: null };
  const citations = list(response.citations);
  const canShowFacts = ["ANSWER", "CONFLICT"].includes(response.gate_route);
  return { structured: structured(response.structured_result, citations, canShowFacts), claims: claims(response.claim_result, citations, canShowFacts) };
}

module.exports = { temporal, presentChatResults };
