"""Deterministic evidence for selection from an already-authorized record corpus."""
from dataclasses import dataclass
import re
from typing import Literal

from app.ai.gates.textrules import content_tokens, normalize_query
from app.ai.query_normalization import is_speech_act


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    record_id: str
    title: str
    text: str
    event_ids: tuple[str, ...] = ()
    occurred_at: str = ""
    location: str = ""
    people: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateQuery:
    text: str
    records: tuple[CandidateRecord, ...]
    eligible_ids: tuple[str, ...]
    ranked_ids: tuple[str, ...] = ()
    limit: int = 3


@dataclass(frozen=True, slots=True)
class CandidateMatch:
    record_id: str
    matched_terms: tuple[str, ...]
    missing_terms: tuple[str, ...]
    matched_fields: tuple[str, ...]
    linked_event_ids: tuple[str, ...]
    score: float


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    mode: Literal["exact", "related", "ambiguous", "none"]
    matches: tuple[CandidateMatch, ...]
    searched_records: int
    query_terms: tuple[str, ...]


_REQUEST_WORDS = {"사진", "이미지", "음성", "영상", "보여줘", "보내줘", "찾아줘", "보여", "보내",
                  "찾아", "관련", "그때", "당시", "좀", "때", "있는", "했던", "했을", "모든", "전체", "날짜",
                  "녹음", "오디오", "비디오", "원본", "들려줘", "들려", "재생", "재생해줘", "틀어줘",
                  "보여주세요", "들려주세요", "찾아주세요", "동영상", "목소리", "파일", "녹음한", "녹음된", "하나", "하나만"}
_ENDING = re.compile(r"(했을때|했을|했었던|했던|했어|했을까|하는|했는지|할때|했을때의|하고|했다가|했다|하며|해서)$")


def _terms(text: str) -> set[str]:
    normalized = normalize_query(text).lower()
    normalized = re.sub(r"(?<=[가-힣])(사진|이미지|동영상|영상|음성|녹음)", r" \1", normalized)  # '경주사진'
    normalized = re.sub(r"(?<![가-힣])보고\s*싶[가-힣]*", " ", normalized)  # '사진 보고 싶어' is a request
    normalized = re.sub(r"(\d{4})년", r"\1", normalized)
    # '오사카서' is the spoken '오사카에서'; applied to query and record fields alike, so nouns ending in 서 still match.
    normalized = " ".join(re.sub(r"(?<=[가-힣]{2})서$", "", _ENDING.sub("", word)) for word in normalized.split())
    return {term for term in content_tokens(normalized) - _REQUEST_WORDS if not is_speech_act(term, movement=True)}


def verify_record_candidates(request: CandidateQuery) -> CandidateSelection:
    """Verify lexical support and event links; missing words never authorize new records."""
    records = {record.record_id: record for record in request.records}
    terms = _terms(request.text)
    names = {term for record in records.values() for name in record.people for term in _terms(name)}
    anchors = terms - names
    direct: dict[str, tuple[set[str], set[str], float]] = {}
    for rid, record in records.items():
        fields = {"title": record.title, "text": record.text, "location": record.location,
                  "occurred_at": record.occurred_at, "people": " ".join(record.people)}
        matched: set[str] = set()
        matched_fields: set[str] = set()
        score = 0.0
        for field, value in fields.items():
            supported = terms & _terms(value)
            if field == "location":  # compound place names: '소극장' is in '대학로소극장'
                supported |= {term for term in terms if len(term) >= 2 and term in value}
            if supported:
                matched.update(supported)
                matched_fields.add(field)
                score += len(supported) * (3 if field in {"title", "location"} else 1)
        title_terms = _terms(record.title)
        if title_terms and title_terms <= terms:
            score += 6
        direct[rid] = matched, matched_fields, score

    # Only the best-supported event anchors transfer evidence to their own media.
    coverage = {rid: len(matched & anchors) for rid, (matched, _, _) in direct.items()}
    strongest = max(coverage.values(), default=0)
    event_support: dict[str, tuple[set[str], float]] = {}
    if strongest:
        for rid, (matched, fields, score) in direct.items():
            if coverage[rid] != strongest or not fields & {"title", "location"}:
                continue
            for event_id in records[rid].event_ids:
                previous_terms, previous_score = event_support.get(event_id, (set(), 0.0))
                event_support[event_id] = previous_terms | matched, max(score, previous_score)

    rank = {rid: index for index, rid in enumerate(dict.fromkeys(request.ranked_ids))}
    matches: list[CandidateMatch] = []
    for rid in dict.fromkeys(request.eligible_ids):
        if rid not in direct:
            continue
        matched, fields, score = direct[rid]
        matched, fields = set(matched), set(fields)
        links = tuple(event_id for event_id in records[rid].event_ids if event_id in event_support)
        for event_id in links:
            evidence, support_score = event_support[event_id]
            matched.update(evidence)
            fields.add("linked_event")
            score = max(score, support_score)
        if strongest and len(matched & anchors) < strongest:
            continue
        if not matched and rid not in rank:
            continue
        matches.append(CandidateMatch(rid, tuple(sorted(matched)), tuple(sorted(terms - matched)),
                                      tuple(sorted(fields)), links, score))
    titles = [rid for rid in direct if len(anchors) > 1 and anchors <= _terms(records[rid].title)
              and _terms(records[rid].title) <= terms]
    if len(titles) == 1:
        target = records[titles[0]]
        focused = [item for item in matches if item.record_id == target.record_id
                   or set(records[item.record_id].event_ids) & set(target.event_ids)]
        if focused:
            matches = focused
    if not matches:
        for rid in rank:
            if rid not in request.eligible_ids or rid not in direct:
                continue
            matched, fields, score = direct[rid]
            matches.append(CandidateMatch(rid, tuple(sorted(matched)), tuple(sorted(terms - matched)),
                                          tuple(sorted(fields)), (), score))
    matches.sort(key=lambda item: (-len(set(item.matched_terms) & anchors), -item.score,
                                   rank.get(item.record_id, len(rank)), item.record_id))
    limit = max(1, request.limit)
    mode: Literal["exact", "related", "ambiguous", "none"] = "none"
    if matches:
        mode = "ambiguous"
        best = matches[0]
        complete = not best.missing_terms and bool(anchors)
        peers = [item for item in matches if not item.missing_terms] if complete else matches
        common_events = set(records[best.record_id].event_ids)
        for item in peers:
            common_events.intersection_update(records[item.record_id].event_ids)
        if complete and (len(peers) == 1 or common_events):
            mode = "related" if "linked_event" in best.matched_fields else "exact"
            matches = [item for item in matches if not item.missing_terms]
    return CandidateSelection(mode, tuple(matches[:limit]), len(records), tuple(sorted(terms)))
