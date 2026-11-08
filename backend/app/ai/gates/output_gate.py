"""출력 게이트 (Phase 1) — LLM 응답을 사용자에게 보내기 전 결정론 검증.

누수 제거 → 한국어 판정 → 문장 단위 hard-atom 근거 대조 → 실패 시 인용 강등.
환각 원자(기록에 없는 숫자·시각·인용구)를 담은 문장은 구조적으로 통과하지 못한다.
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ai.grounded_answering import AnswerPlan

from app.ai.gates.entity_index import KINSHIP, PLACE_SUFFIXES, RoomEntityIndex
from app.ai.gates.textrules import (
    content_tokens,
    fold_family_terms,
    korean_ok,
    list_format_markers,
    strip_leaks,
    unsupported_atoms,
)


def validate_plan_selection(raw_output: str, plan: AnswerPlan) -> tuple[str, ...] | None:
    """Accept only a complete permutation of server-approved claim IDs."""
    if not isinstance(raw_output, str) or len(raw_output) > 8192:
        return None

    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    content = raw_output.strip()
    fence = chr(96) * 3
    if content.startswith(fence) or content.endswith(fence):
        pattern = rf"{re.escape(fence)}(json)?[ \t]*\r?\n(.*?)\r?\n{re.escape(fence)}"
        match = re.fullmatch(pattern, content, flags=re.DOTALL)
        if match is None:
            return None
        content = match.group(2)

    try:
        parsed = json.loads(content, object_pairs_hook=unique_keys)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"claim_ids"}:
        return None
    ids = parsed["claim_ids"]
    expected = {claim.claim_id for claim in plan.claims}
    if (not isinstance(ids, list) or not ids or len(ids) != len(expected)
            or not all(isinstance(item, str) for item in ids)
            or len(set(ids)) != len(ids) or set(ids) != expected):
        return None
    return tuple(ids)


def _unsupported_entities(sentence: str, allowed_source: str,
                          index: RoomEntityIndex) -> set[str]:
    """이름 있는 장소 토큰·X씨/X님 인물 호칭이 (기록∪가족방 사전)에 없으면 미지원."""
    bad: set[str] = set()
    for token in content_tokens(sentence):
        for suffix in PLACE_SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix):
                if token not in allowed_source and token not in index.places:
                    bad.add(token)
    for m in re.finditer(r"([가-힣]{2,4})(씨|님)", sentence):
        name = m.group(1)
        if name not in allowed_source and name not in index.persons:
            bad.add(m.group())
    return bad

FALLBACK_TMPL = ("생성된 답변에서 기록으로 확인되지 않는 내용을 제외했습니다. "
                 "현재 남아 있는 기록은 다음과 같습니다: {quotes}")

# '기록에 있다/말했다'고 출처를 주장하는 표현 — 주장한 내용이 실제 기록과 접점이 있어야 한다.
_PROVENANCE_RE = re.compile(r"기록되|기록에|적혀\s*있|남아\s*있|라고\s*(?:하|말|적)|말씀하")

# 가족 구성원에 대한 감정·관계·기원 술어 — 출현별 '양태'가 사실 주장 여부를 결정한다.
# (체크포인트 10 감사 1: '응원/축하'도 과거형이면 사실 보고다. 절 분리·문장 면제는
#  쉼표/붙여쓰기/다른 연결어미로 우회되므로 폐기 — 어간 출현 단위로 판정한다.)
_PREDICATE_STEMS = ("사랑", "미워", "미움", "아끼", "예뻐하", "귀여워하", "좋아하", "싫어하",
                    "자랑스러", "서운", "원망", "그리워", "존경", "질투",
                    "응원", "축하", "축복")
# 수행·기원 양태: '응원할게요/축하드립니다/행복하길 바랍니다' — 사실 주장이 아니다
_PERFORMATIVE_TAIL_RE = re.compile(
    r"할게|해\s*드리|해\s*드려|드립|드려|하길|하시길|기를|되길|되시길|빕니다|바라|바랍|기원|소망")
# 과거 사실형: 어간 직후 '했' — '응원했어요/축하했어요'는 과거 사실 보고다
_PAST_TAIL_RE = re.compile(r"^\s*했")


def _occurrence_is_wish(tail: str) -> bool:
    """어간 출현 하나의 양태 — 과거형이면 사실, 수행·기원 표지가 있으면 기원,
    증명 불가면 사실 주장으로 취급해 검증한다 (감사 10-1)."""
    if _PAST_TAIL_RE.match(tail):
        return False
    return bool(_PERFORMATIVE_TAIL_RE.search(tail[:20]))


def _occurrence_polarity(head: str, tail: str) -> str:
    negated = bool(re.match(r"\s*(?:하지\s*않|지\s*않|하지\s*못|지\s*못)", tail)) \
        or head.endswith("안 ") or head.endswith("못 ")
    return "neg" if negated else "pos"


def _stem_polarities(text: str, stem: str) -> set[str]:
    """어간의 긍/부정 극성 집합 — '사랑하지 않'은 neg, '사랑한다'는 pos."""
    polarities: set[str] = set()
    for m in re.finditer(re.escape(stem), text):
        polarities.add(_occurrence_polarity(text[max(0, m.start() - 3):m.start()],
                                            text[m.end():m.end() + 6]))
    return polarities


def _unsupported_emotion_claim(sentence: str, evidence_joined: str,
                               index: RoomEntityIndex) -> bool:
    """가족 구성원에 대한 감정·관계 술어 대조 (감사 8-A / 9-1 / 10-1).

    어간 '출현'마다: 기원 양태(응원할게/축하드립니다/…기를 바랍니다)면 창작으로 면제,
    과거형·증명 불가 양태는 사실 주장으로 보고 (a) 어간이 기록에 없으면 차단,
    (b) 있어도 극성이 다르면(부정 반전) 차단. 위치·표기(쉼표/붙여쓰기/연결어미)에
    무관한 판정이라 절 분리 우회가 없다. 인물 언급 없는 문장은 대상 아님.
    """
    folded_sentence = fold_family_terms(sentence)
    if not any(p and p in folded_sentence for p in set(index.persons) | set(KINSHIP)):
        return False
    folded_evidence = fold_family_terms(evidence_joined)
    for stem in _PREDICATE_STEMS:
        for m in re.finditer(re.escape(stem), folded_sentence):
            tail = folded_sentence[m.end():]
            if _occurrence_is_wish(tail):
                continue
            if stem not in folded_evidence:
                return True
            occurrence = _occurrence_polarity(
                folded_sentence[max(0, m.start() - 3):m.start()], tail)
            if {occurrence} - _stem_polarities(folded_evidence, stem):
                return True
    return False


def _provenance_claim_unsupported(sentence: str, evidence_joined: str,
                                  index: RoomEntityIndex) -> bool:
    """출처 주장 문장의 근거 대조 (Phase 5: UX-G12 — 감정·관계 환각).

    "X를 가장 사랑했다고 기록되어 있습니다"처럼 숫자·인용이 없는 의미 주장도,
    '기록되어 있다'고 말하는 순간 검증 가능한 사실 주장이 된다. 인물 호칭을 제외한
    실질 내용어가 기록과 하나도 겹치지 않으면 그 출처 주장은 거짓이다.
    감정 표현 자체는 막지 않는다 — 출처를 참칭하는 문장만 막는다 (창작·덕담은 통과).
    """
    if not _PROVENANCE_RE.search(sentence):
        return False
    persons = set(index.persons) | set(KINSHIP)
    sentence_tokens = {t for t in content_tokens(fold_family_terms(sentence))
                       if t not in persons}
    evidence_tokens = content_tokens(fold_family_terms(evidence_joined))
    return not (sentence_tokens & evidence_tokens)


@dataclass
class GateResult:
    answer: str
    action: str            # pass | dropped_N | fallback_quote | all_dropped_quote
    leak_stripped: bool
    dropped_sentences: int
    unsupported_found: list[str]


def _quote_snippets(evidence_texts: list[str], limit: int = 2) -> str:
    return " / ".join(f"「{t[:42]}…」" if len(t) > 42 else f"「{t}」"
                      for t in evidence_texts[:limit])


def apply_output_gate(raw_answer: str, query: str, evidence_texts: list[str],
                      entity_index: RoomEntityIndex | None = None) -> GateResult:
    index = entity_index or RoomEntityIndex()
    evidence_joined = " ".join(evidence_texts)
    # Phase 0: 질의는 신뢰 소스가 아니다 — 질의 속 값은 검증 대상 주장일 뿐,
    # 증거∪가족방 사전만 허용한다 (query 인자는 API 호환용으로 유지)
    allowed_source = evidence_joined
    text, leaked = strip_leaks(raw_answer or "")

    if not text or not korean_ok(text):
        return GateResult(
            answer=FALLBACK_TMPL.format(quotes=_quote_snippets(evidence_texts)),
            action="fallback_quote", leak_stripped=leaked,
            dropped_sentences=0, unsupported_found=[])

    sentences = re.split(r"(?<=[.다요!?])\s+", text)
    # 출력 '형식' 번호(1. 2. 3. …)는 전체 답변 기준으로 판정 — 사실 값 대조에서 제외 (UX-G08)
    format_markers = list_format_markers(text)
    kept: list[str] = []
    dropped = 0
    found: list[str] = []
    for sentence in sentences:
        checked = sentence
        for marker in sorted(format_markers, key=len, reverse=True):
            checked = checked.replace(marker, " ")
        bad = unsupported_atoms(checked, evidence_joined)
        bad |= _unsupported_entities(sentence, allowed_source, index)
        if not bad and _provenance_claim_unsupported(sentence, evidence_joined, index):
            bad = {"provenance_claim"}
        if not bad and _unsupported_emotion_claim(sentence, evidence_joined, index):
            bad = {"emotion_claim"}
        if bad:
            dropped += 1
            found.extend(sorted(bad))
            continue
        kept.append(sentence)

    if not kept:
        return GateResult(
            answer=FALLBACK_TMPL.format(quotes=_quote_snippets(evidence_texts)),
            action="all_dropped_quote", leak_stripped=leaked,
            dropped_sentences=dropped, unsupported_found=found)

    return GateResult(
        answer=" ".join(kept).strip(),
        action="pass" if dropped == 0 else f"dropped_{dropped}",
        leak_stripped=leaked, dropped_sentences=dropped, unsupported_found=found)
