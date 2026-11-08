"""입력 래더 (Phase 2) — 질문이 기록으로 답변 가능한지 LLM 호출 전에 규칙으로 판정.

SCPC control 래더의 직계: ANSWER=proceed, QUOTE=amend, REFUSE=hold, CLARIFY=ask.
REFUSE/CLARIFY/CONFLICT 경로는 LLM을 호출하지 않으므로 해당 경로 할루시네이션은 구조적으로 0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.gates.decision_rules import (
    decision_statements, decision_topics, opposite_commitments, requests_decision,
    years_compatible,
)

from app.ai.gates.entity_index import (
    KINSHIP,
    RoomEntityIndex,
    has_any_person,
    has_grounded_place_evidence,
    has_named_place_of,
    question_place_category,
)
from app.ai.gates.counts import has_requested_count
from app.ai.gates.object_facts import object_request, object_supported
from app.ai.gates.location_facts import location_request, location_supported
from app.ai.gates.textrules import (
    content_tokens,
    evidence_excerpt,
    fold_family_terms,
    has_cue,
    time_atoms,
    time_display,
    time_intervals,
)

FACT_CUES = ("언제", "몇 시", "몇시", "며칠", "몇 월", "몇월", "몇 년", "몇년", "어디", "어느",
             "누구", "누가", "이름", "얼마", "몇 명", "몇명", "날짜", "시각", "장소")

REFUSE_TMPL = ("기록을 확인했지만, 물어보신 내용은 현재 가족 기록에서 확인되지 않습니다. "
               "관련해서 남아 있는 기록은 다음과 같습니다: {quotes}")
CLARIFY_TMPL = "어떤 기록을 말씀하시는지 조금 더 구체적으로 알려주시면 그 기록을 기준으로 답하겠습니다."
NO_RECORD_TMPL = "아직 이 가족방에 관련 기록이 없어, 물어보신 내용을 기록으로 답해 드리기 어렵습니다."
CONFLICT_TMPL = ("기록이 서로 다르게 남아 있어 단정하지 않겠습니다. "
                 "기록마다 {values}로 다르게 적혀 있습니다. 확인 후 맞춰두는 것이 좋겠습니다.")
AMBIGUOUS_PERSON_TMPL = ("가족 기록에 여러 분이 함께 나와요 — {names}. "
                         "어느 분 이야기인지 알려주시면 그 기록을 기준으로 답하겠습니다.")
AMBIGUOUS_EVENT_TMPL = ("서로 다른 일정 기록이 있어요 — {events}. "
                        "어느 일정 말씀인지 알려주시면 그 기록을 기준으로 답하겠습니다.")

# 사건 판별에서 제외하는 일반 일정 어휘 — '예약/안내/문자'는 사건을 특정하지 못한다
_GENERIC_EVENT_WORDS = frozenset({
    "예약", "안내", "문자", "메모", "변경", "확인", "리마인더", "예정", "확정",
    "시간", "도착", "시작", "요청", "내원", "접수", "오전", "오후", "일정", "사진",
    "시야",  # '몇 시야?'의 조사 잔여 — 사건어가 아니다
})


@dataclass
class RouteDecision:
    route: str                    # ANSWER | REFUSE | CLARIFY | CONFLICT | NO_RECORD
    answer: str | None = None     # ANSWER 외 경로의 결정론 응답
    asked_kinds: list[str] = field(default_factory=list)
    detail: str = ""


def asked_atom_kinds(query: str) -> set[str]:
    kinds: set[str] = set()
    if has_cue(query, ("몇 시", "시각", "몇 분")):
        kinds.add("time")
    # 날짜는 세분화: 월/일을 물으면 연도만 있는 기록으로는 답할 수 없다
    if has_cue(query, ("며칠", "몇 월")):
        kinds.add("date_md")
    if has_cue(query, ("몇 년", "년도")):
        kinds.add("date_y")
    if has_cue(query, ("날짜", "언제")) and not (kinds & {"date_md", "date_y"}):
        kinds.add("date_any")
    if has_cue(query, ("얼마", "몇 명", "몇 사람", "몇 개", "몇 스푼")):
        kinds.add("number")
    if has_cue(query, ("이름", "어디", "어느", "장소", "누가", "누구")):
        kinds.add("entity")
    # Specific time/date/entity questions keep their established contracts.
    if not kinds and requests_decision(query):
        kinds.add("decision")
    if not kinds and object_request(query) is not None:
        kinds.add("object")
    return kinds


def _entity_answerable(query: str, evidence_texts: list[str],
                       index: RoomEntityIndex) -> tuple[bool, str]:
    """엔티티 질문의 답변 가능성 — 가족방 사전 기반 (v2: 얕은 접점 판정을 대체).

    - '식당/병원/공원…' 카테고리 이름 질문 → 그 카테고리의 '이름 있는' 장소가 있어야 함
    - '누가/누구' 인물 질문 → 기록/사전에 인물(멤버 이름·호칭)이 등장해야 함
    - 일반 '어디' 질문 → 아무 장소 신호(접미사 토큰)라도 있으면 답변 가능
    """
    category = question_place_category(query)
    if category is not None:
        ok = has_named_place_of(category, index, evidence_texts)
        return ok, f"place_category={category[0]}"
    if any(word in query for word in ("누가", "누구")):
        return has_any_person(index, evidence_texts), "person"
    if any(word in query for word in ("어디", "장소")):
        if location_request(query) is not None:
            return any(location_supported(query, text) for text in evidence_texts), "object_location"
        ok = has_grounded_place_evidence(evidence_texts)
        return ok, "generic_place"
    # '이름' 단독 등 — 판별 불가면 보수적으로 접점 판정에 위임
    joined = " ".join(evidence_texts)
    return len(content_tokens(query) & content_tokens(joined)) >= 1, "fallback_overlap"


def _evidence_has_kind(kind: str, query: str, evidence_text: str) -> bool:
    if kind == "object":
        return object_supported(query, evidence_text)
    if kind == "decision":
        return bool(decision_statements(query, evidence_text, (*_EVENT_NOUNS, *KINSHIP)))
    if kind == "time":
        return bool(re.search(r"\d{1,2}\s*시|\d{1,2}:\d{2}", evidence_text))
    if kind == "date_md":
        return bool(re.search(r"\d{1,2}\s*월\s*\d{1,2}\s*일", evidence_text))
    if kind == "date_y":
        return bool(re.search(r"\d{4}\s*년", evidence_text))
    if kind == "date_any":
        return bool(re.search(r"\d{4}\s*년|\d{1,2}\s*월\s*\d{1,2}\s*일", evidence_text))
    if kind == "number":
        if has_cue(query, ("몇 명", "몇 사람", "몇 개")):
            return has_requested_count(query, evidence_text)
        return has_requested_count(query, evidence_text) or bool(
            re.search(r"\d+\s*(만원|원|스푼|분)", evidence_text))
    if kind == "entity":
        if location_request(query) is not None:
            return location_supported(query, evidence_text)
        # 엔티티는 사전 없인 판별이 얕다(v1 한계). 질의 내용어와의 접점만 요구한다.
        return len(content_tokens(query) & content_tokens(evidence_text)) >= 1
    return False


def _chunk_persons(text: str, index: RoomEntityIndex) -> set[str]:
    """chunk에 언급된 인물(가족방 사전 인명 + 호칭) — 긴 호칭 우선(외할아버지⊃할아버지 방지).

    호칭 변형(엄마/어머니, 아빠/아버지)은 fold_family_terms로 접어 한 사람으로 센다.
    """
    found: set[str] = set()
    masked = fold_family_terms(text or "")
    for term in sorted(set(index.persons) | set(KINSHIP), key=len, reverse=True):
        if term and term in masked:
            found.add(term)
            masked = masked.replace(term, " ")
    return found


def evidence_matches_query_person(query: str, text: str, index: RoomEntityIndex) -> bool:
    """Keep uncertain/shared references; exclude only explicitly different people."""
    query_persons = _chunk_persons(query, index)
    persons = _chunk_persons(text, index)
    return not query_persons or not persons or bool(query_persons & persons)


def _chunk_events(text: str, index: RoomEntityIndex) -> set[str]:
    """chunk의 사건·술어 토큰 — 인물·시각·일반 일정 어휘를 뺀 실질 사건어 (치과, 건강검진, 생일…)."""
    persons = set(index.persons) | set(KINSHIP)
    events: set[str] = set()
    for token in content_tokens(fold_family_terms(text)):
        if token in persons or token in _GENERIC_EVENT_WORDS:
            continue
        if re.search(r"\d", token):
            continue
        events.add(token)
    return events


# 사건 동치는 일반 토큰 부분일치가 아니라 '타입 시그니처'로 판정한다 (감사 9-3).
# 진료과(구분자)·검사류(동의 계열)·일반 수식어는 한국어 일반 어휘 — 가족 특정 데이터 아님.
_DEPARTMENT_TERMS = ("치과", "안과", "내과", "피부과", "이비인후과", "정형외과",
                     "산부인과", "소아과", "외과", "비뇨기과")
_EXAM_HEAD_TERMS = ("검진", "검사", "진료")
_GENERIC_EVENT_MODIFIERS = ("종합", "건강", "정기")
# 사건을 실제로 특정하는 도메인 사건 명사 — 임의 명사(병원, OCR…)로 '다른 사건' 판정을
# 내리지 않기 위한 한정 사전 (한국어 일반 어휘, 가족 특정 데이터 아님)
_EVENT_NOUNS = ("생일", "여행", "송년회", "결혼식", "제사", "졸업식", "입학식",
                "돌잔치", "명절", "설날", "추석", "휴가", "소풍", "나들이", "예방접종")


def evidence_scope_matches(query: str, text: str, index: RoomEntityIndex) -> bool:
    """Shared admission/router scope checks; explicit years for decisions only."""
    if not evidence_matches_query_person(query, text, index):
        return False
    left = _event_signature(_chunk_events(query, index))
    right = _event_signature(_chunk_events(text, index))
    if left[0] and right[0] and not left[0] & right[0]:
        return False
    a, b = left[1] | left[2], right[1] | right[2]
    if a and b and not a & b:
        return False
    return "decision" not in asked_atom_kinds(query) or years_compatible(query, text)


def _decision_support(query, text, index):
    if not evidence_scope_matches(query, text, index):
        return []
    return [s for s in decision_statements(query, text, (*_EVENT_NOUNS, *KINSHIP, *index.persons))
            if evidence_scope_matches(query, s, index)]


def _decision_conflict(query, evidence_texts, index):
    statements = [(text, s) for text in evidence_texts for s in _decision_support(query, text, index)]
    for i, (left_text, left) in enumerate(statements):
        for right_text, right in statements[i + 1:]:
            if (evidence_scope_matches(left_text, right_text, index)
                    and years_compatible(left_text, right_text)
                    and decision_topics(left, (*_EVENT_NOUNS, *KINSHIP, *index.persons))
                    & decision_topics(right, (*_EVENT_NOUNS, *KINSHIP, *index.persons))
                    and opposite_commitments(left, right)):
                return True
    return False


def _event_signature(events: set[str]) -> tuple[frozenset, frozenset, frozenset]:
    """(진료과, 검사류 헤드, 도메인 사건 명사) — 종합검진/건강검사는 같은 시그니처가 된다.

    사전 밖 임의 명사는 시그니처에 넣지 않는다 — 우발적 토큰 차이(병원/OCR/메모)로
    실모순을 '다른 사건'이라 오판하면 안 된다 (감사 9-3: 부분일치 금지의 양방향 적용).
    """
    departments: set[str] = set()
    heads: set[str] = set()
    nouns: set[str] = set()
    for token in events:
        departments |= {d for d in _DEPARTMENT_TERMS if d in token}
        if any(h in token for h in _EXAM_HEAD_TERMS):
            heads.add("exam")
        nouns |= {n for n in _EVENT_NOUNS if n in token}
    return frozenset(departments), frozenset(heads), frozenset(nouns)


def _event_relation(events_a: set[str], events_b: set[str]) -> str:
    """두 chunk 사건의 관계 — "same"(모순 비교 가능) / "different"(무관) / "uncertain"(보수 CLARIFY).

    - 진료과가 서로 다르면 확실히 다른 일정 (치과 vs 안과)
    - 한쪽만 진료과를 명시하면 불확실 (치과 검진 vs 건강 검진 — 거짓 모순 금지)
    - 둘 다 무과 + 검사류면 같은 부류 (종합검진 == 건강검사 — 실모순 유지)
    - 도메인 사건 명사(생일/여행)가 서로 다르면 다른 사건
    - 시그니처가 비어 있으면(사전 밖 명사뿐) 구분 불가 → 비교 허용(보수적 모순 유지)
    """
    if not events_a or not events_b:
        # 사건어 자체가 없는 쪽(안내/변경 문자 등 후속 메시지)은 같은 맥락으로 본다 —
        # B04/G05형 실모순 유지 (사건어가 '있는데' 미식별인 경우와 구분)
        return "same"
    sig_a = _event_signature(events_a)
    sig_b = _event_signature(events_b)
    if not any(sig_a) and not any(sig_b):
        # 둘 다 사전 밖 사건어(낚시 모임/독서 모임 등) — 같은 사건이라고 적극 확인될
        # 때(사건어 집합 동일)만 same, 아니면 uncertain (감사 10-2: 거짓 모순 금지)
        return "same" if events_a == events_b else "uncertain"
    if not any(sig_a) or not any(sig_b):
        return "uncertain"  # 한쪽만 식별됨 (가족 여행 vs 사전 밖 모임)
    dep_a, head_a, noun_a = sig_a
    dep_b, head_b, noun_b = sig_b
    if dep_a and dep_b and not (dep_a & dep_b):
        return "different"
    if bool(dep_a) != bool(dep_b):
        return "uncertain"
    if noun_a and noun_b and not (noun_a & noun_b):
        return "different"
    if bool(noun_a) != bool(noun_b) and (head_a or head_b):
        return "uncertain"
    return "same"


def _detect_time_conflict(evidence_texts: list[str], index: RoomEntityIndex,
                          query: str = "") -> list[str] | None:
    """시각 모순 — '같은 대상'의 서로 다른 chunk 시각이 같은 시각일 수 없을 때만 모순.

    Phase 0: 첫 2개 chunk만 비교하던 방식은 10시/10시/11시를 놓쳤다 → 전 chunk 대조.
    Phase 2: 하루-분 후보 집합(time_atoms) 교집합 판정 — 오전2시/오후2시는 모순,
    오후2시/14:00은 동치. 단일 chunk 내 범위 표현은 기록 간 모순이 아니다.
    Phase 3: 서로 다른 인물의 일정은 비교하지 않는다(거짓 모순 방지). 범위 기록과
    그 안의 점값은 양립. 충돌값은 처음 두 개가 아니라 '전부' 반환한다.
    """
    query_events = _chunk_events(query, index) if query else set()
    chunks = []
    for text in evidence_texts:
        intervals = time_intervals(text)
        masked = text
        for surface, _ in intervals:
            masked = masked.replace(surface, " ")
        points = time_atoms(masked)
        if not (intervals or points):
            continue
        events = _chunk_events(text, index)
        # 감사 10-3: 모순 탐지는 질의와 관련된 기록끼리만 — 질의가 사건을 말했는데
        # 그 사건과 확실히 무관(different)한 chunk는 비교 대상에서 제외한다
        if query_events and events and _event_relation(query_events, events) == "different":
            continue
        chunks.append({"persons": _chunk_persons(text, index),
                       "events": events,
                       "intervals": intervals, "points": points})

    def comparable(a: dict, b: dict) -> bool:
        # 두 chunk가 서로 다른 인물만 언급하면 같은 일정이 아니다
        if a["persons"] and b["persons"] and not (a["persons"] & b["persons"]):
            return False
        # 사건 시그니처가 "same"일 때만 모순 비교 — different(치과/안과)와
        # uncertain(치과 검진/건강 검진)은 모순으로 묶지 않는다 (감사 8-C, 9-3)
        return _event_relation(a["events"], b["events"]) == "same"

    def point_fits_interval(cands: frozenset[int], pairs: list[tuple[int, int]]) -> bool:
        return any(lo <= p <= hi for p in cands for lo, hi in pairs)

    def intervals_overlap(pa: list[tuple[int, int]], pb: list[tuple[int, int]]) -> bool:
        return any(lo_a <= hi_b and lo_b <= hi_a
                   for lo_a, hi_a in pa for lo_b, hi_b in pb)

    conflict_displays: list[str] = []

    def add_display(display: str) -> None:
        if display not in conflict_displays:
            conflict_displays.append(display)

    for i in range(len(chunks)):
        for j in range(i + 1, len(chunks)):
            a, b = chunks[i], chunks[j]
            if not comparable(a, b):
                continue
            for surface_a, cands_a in a["points"]:
                for surface_b, cands_b in b["points"]:
                    if not (cands_a & cands_b):
                        add_display(time_display(surface_a, cands_a))
                        add_display(time_display(surface_b, cands_b))
            for surface_p, cands_p in a["points"]:
                for surface_i, pairs in b["intervals"]:
                    if not point_fits_interval(cands_p, pairs):
                        add_display(time_display(surface_p, cands_p))
                        add_display(surface_i)
            for surface_i, pairs in a["intervals"]:
                for surface_p, cands_p in b["points"]:
                    if not point_fits_interval(cands_p, pairs):
                        add_display(surface_i)
                        add_display(time_display(surface_p, cands_p))
            for surface_ia, pairs_a in a["intervals"]:
                for surface_ib, pairs_b in b["intervals"]:
                    if not intervals_overlap(pairs_a, pairs_b):
                        add_display(surface_ia)
                        add_display(surface_ib)

    return conflict_displays if len(conflict_displays) >= 2 else None


def _ambiguous_person(query: str, evidence_texts: list[str], index: RoomEntityIndex,
                      kinds: set[str]) -> list[str] | None:
    """질문에 대상 인물이 없고, 물은 값이 서로 다른 인물 기록에 각각 있으면 모호 —
    한 사람을 임의로 골라 답하면 안 된다 (UX-G07)."""
    if not kinds or _chunk_persons(query, index):
        return None
    per_person: set[str] = set()
    for text in evidence_texts:
        persons = _chunk_persons(text, index)
        if len(persons) == 1 and any(_evidence_has_kind(k, query, text) for k in kinds):
            per_person.add(next(iter(persons)))
    return sorted(per_person) if len(per_person) >= 2 else None


def _ambiguous_event(query: str, evidence_texts: list[str], index: RoomEntityIndex,
                     kinds: set[str]) -> list[str] | None:
    """질문이 사건을 특정하지 않았는데 값이 서로 same이 아닌 사건 기록들에 있으면 모호 —
    임의 사건을 골라 답하거나 모순으로 묶으면 안 된다 (감사 8-C / 9-3).

    질의가 '진료과'를 명시하고 그 과와 일치하는 기록군이 정확히 하나면 특정으로 본다
    (치과 검진 질문 → 치과 기록으로 답변). 진료과 없는 '검진' 질의는 치과 검진과
    건강 검진을 구분하지 못하므로 특정이 아니다.
    """
    if not kinds:
        return None
    query_events = _chunk_events(query, index)
    query_departments = _event_signature(query_events)[0] if query_events else frozenset()
    groups: list[set[str]] = []
    for text in evidence_texts:
        if not evidence_matches_query_person(query, text, index):
            continue
        events = _chunk_events(text, index)
        if not events or not any(_evidence_has_kind(k, query, text) for k in kinds):
            continue
        groups.append(events)
    if query_departments:
        matched = [g for g in groups
                   if _event_signature(g)[0] & query_departments]
        if len(matched) == 1:
            return None  # 진료과로 특정됨 (UX-A07)
    if query_events:
        # 감사 10-3: 진료과만이 아니라 질의 사건 시그니처 전체로 스코프를 좁힌다 —
        # 질의와 same인 기록군이 정확히 하나이고 나머지가 전부 different면 특정된 것.
        # (uncertain이 남으면 특정 실패 — 검진 질의는 치과/건강 검진을 못 가른다)
        same_groups = [g for g in groups
                       if _event_relation(query_events, g) == "same"]
        other_groups = [g for g in groups
                        if _event_relation(query_events, g) != "same"]
        if len(same_groups) >= 1 and all(
                _event_relation(query_events, g) == "different" for g in other_groups):
            return None  # 사건으로 특정됨 (UX-A19)
    distinct: list[set[str]] = []
    for events in groups:
        # 사전 밖 사건(낚시 모임 등)도 그룹으로 인정 — 시그니처가 비었다고 버리면
        # 미지 사건 다수가 조용히 ANSWER로 새어간다 (감사 10-2: 보수적 CLARIFY)
        if not any(_event_relation(events, seen) == "same" for seen in distinct):
            distinct.append(events)
    if len(distinct) >= 2:
        labels: set[str] = set()
        for events in distinct:
            departments, _heads, nouns = _event_signature(events)
            if departments:
                labels.add(sorted(departments)[0])
            elif nouns:
                labels.add(sorted(nouns)[0])
            else:
                exam_tokens = [t for t in events if any(h in t for h in _EXAM_HEAD_TERMS)]
                labels.add(min(exam_tokens or sorted(events), key=len))
        return sorted(labels)
    return None


def _quote_snippets(evidence_texts: list[str], query: str = "", limit: int = 2) -> str:
    """부분답변 인용 — 고정 앞 42자가 아니라 값(연도·시각·금액)·질의어 주변을 발췌한다 (UX-G14)."""
    return " / ".join(f"「{evidence_excerpt(t, query)}」" for t in evidence_texts[:limit])


_NEGATION_RE = re.compile(r"지\s*않|지\s*못|(?<![가-힣])안\s|(?<![가-힣])못\s|없")

# 의문사 부류 — 이유(왜)를 방법(어떻게)으로 바꾸는 재작성은 의미 보존이 아니다 (감사 9-2)
_QUESTION_WORD_CLASSES = (("어째서", "reason"), ("왜", "reason"),
                          ("어떻게", "manner"), ("어쩌다", "manner"),
                          ("무엇", "what"), ("뭐", "what"))


def _question_word_classes(text: str) -> set[str]:
    return {cls for word, cls in _QUESTION_WORD_CLASSES if word in (text or "")}


def rewrite_preserves_intent(original: str, rewritten: str,
                             index: RoomEntityIndex | None = None) -> bool:
    """재작성 채택 계약 (감사 8-B/9-2) — '표기 복원'임을 증명할 수 있을 때만 True.

    원문에 없던 인물·장소·숫자·'주제 내용어'를 더하거나, 부정을 뒤집거나, 의문사
    부류·질문 종류를 바꾸면 임의 대상 선택이므로 폐기한다. 증명 불가면 CLARIFY 유지.
    (현재 프로덕션 경로는 LLM 재작성을 호출하지 않는다 — 이 함수는 재도입 시의
    채택 계약이자 회귀 테스트 대상으로 유지된다.)
    """
    from app.ai.gates.textrules import normalize_query, num_atoms
    idx = index or RoomEntityIndex()
    if _chunk_persons(rewritten, idx) - _chunk_persons(original, idx):
        return False
    from app.ai.gates.entity_index import PLACE_SUFFIXES

    def place_tokens(text: str) -> set[str]:
        return {t for t in content_tokens(text)
                for s in PLACE_SUFFIXES if t.endswith(s) and len(t) > len(s)}
    if place_tokens(rewritten) - place_tokens(original):
        return False
    if num_atoms(rewritten) - num_atoms(original):
        return False
    if bool(_NEGATION_RE.search(rewritten)) != bool(_NEGATION_RE.search(original)):
        return False
    if asked_atom_kinds(rewritten) != asked_atom_kinds(original):
        return False
    if _question_word_classes(rewritten) != _question_word_classes(original):
        return False
    # 주제 하이재킹 차단: 표기 접기 후에도 원문에 없던 내용어가 생기면 재작성이 아니다
    def topic_tokens(text: str) -> set[str]:
        return content_tokens(fold_family_terms(normalize_query(text)))
    if topic_tokens(rewritten) - topic_tokens(original):
        return False
    return True


def route_query(query: str, evidence_texts: list[str],
                entity_index: RoomEntityIndex | None = None, *, relevance_admitted: bool = False) -> RouteDecision:
    if not evidence_texts:
        return RouteDecision(route="NO_RECORD", answer=NO_RECORD_TMPL, detail="no_evidence")

    index = entity_index or RoomEntityIndex()
    joined = " ".join(evidence_texts)
    kinds = asked_atom_kinds(query)
    is_fact = bool(kinds) or has_cue(query, FACT_CUES)

    if not is_fact:
        from app.ai.gates.stored_memory import opposing, request, subject, support
        if request(query) is not None:
            persons = _chunk_persons(query, index) or subject(query)
            supported = [t for t in evidence_texts
                         if evidence_scope_matches(query, t, index)
                         and support(query, t, persons, (*index.persons, *KINSHIP))]
            if supported and any(opposing(query, t, persons, supported) for t in evidence_texts):
                return RouteDecision(route="CLARIFY", answer=CLARIFY_TMPL,
                                     detail="descriptive_conflict")
            if supported and not persons and len(set().union(*[
                    _chunk_persons(t, index) for t in supported])) > 1:
                return RouteDecision(route="CLARIFY", answer=CLARIFY_TMPL,
                                     detail="ambiguous_person")
            if supported and relevance_admitted:
                return RouteDecision(route="ANSWER", asked_kinds=["stored_memory"],
                                     detail="direct_descriptive_statement")
        return RouteDecision(route="NO_RECORD", answer=NO_RECORD_TMPL,
                             detail="unknown_answerability")

    if "decision" in kinds:
        if _decision_conflict(query, evidence_texts, index):
            return RouteDecision(route="CONFLICT", asked_kinds=sorted(kinds),
                answer="관련 기록에 서로 반대되는 결정이 남아 있어 단정하지 않겠습니다.",
                detail="decision_conflict")
        supported = [t for t in evidence_texts if _decision_support(query, t, index)]
        if not supported:
            return RouteDecision(route="REFUSE", asked_kinds=sorted(kinds),
                answer=REFUSE_TMPL.format(quotes=_quote_snippets(evidence_texts, query)),
                detail="missing_kinds=decision")
        # Unrelated/insufficient admitted neighbors cannot supply decision atoms
        # or create ambiguity; per-quote planning runs this same check again.
        evidence_texts = supported
        joined = " ".join(supported)

    if "time" in kinds:
        conflict_values = _detect_time_conflict(evidence_texts, index, query)
        if conflict_values:
            return RouteDecision(
                route="CONFLICT", asked_kinds=sorted(kinds),
                answer=CONFLICT_TMPL.format(values=" / ".join(conflict_values)),
                detail="time_conflict")

    if is_fact:
        answer_evidence = [text for text in evidence_texts
                           if evidence_matches_query_person(query, text, index)]
        if not answer_evidence:
            return RouteDecision(route="CLARIFY", asked_kinds=sorted(kinds),
                                 answer=CLARIFY_TMPL, detail="low_coverage")
        answer_joined = " ".join(answer_evidence)
        missing: list[str] = []
        for kind in sorted(kinds):
            if kind == "entity":
                ok, why = _entity_answerable(query, answer_evidence, index)
                if not ok:
                    missing.append(f"entity({why})")
            elif not _evidence_has_kind(kind, query, answer_joined):
                missing.append(kind)
        if missing:
            return RouteDecision(
                route="REFUSE", asked_kinds=sorted(kinds),
                answer=REFUSE_TMPL.format(quotes=_quote_snippets(answer_evidence, query)),
                detail=f"missing_kinds={','.join(missing)}")
        ambiguous = _ambiguous_person(query, evidence_texts, index, kinds)
        if ambiguous:
            return RouteDecision(
                route="CLARIFY", asked_kinds=sorted(kinds),
                answer=AMBIGUOUS_PERSON_TMPL.format(names=", ".join(ambiguous)),
                detail="ambiguous_person")
        ambiguous_events = _ambiguous_event(query, evidence_texts, index, kinds)
        if ambiguous_events:
            return RouteDecision(
                route="CLARIFY", asked_kinds=sorted(kinds),
                answer=AMBIGUOUS_EVENT_TMPL.format(events=", ".join(ambiguous_events)),
                detail="ambiguous_event")
        # 접점 판정도 호칭·경어 접기 후 비교 — '어머니 생신' 질문이 '엄마 생일' 기록과
        # 접점 0으로 판정되면 불필요한 재작성 왕복이 생긴다 (UX-G01)
        if not relevance_admitted and not (content_tokens(fold_family_terms(query))
                & content_tokens(fold_family_terms(joined))):
            return RouteDecision(route="CLARIFY", asked_kinds=sorted(kinds),
                                 answer=CLARIFY_TMPL, detail="low_coverage")

    return RouteDecision(route="ANSWER", asked_kinds=sorted(kinds))
