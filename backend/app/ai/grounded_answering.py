"""Transient, extractive answer contracts. A quote is record content, not confirmed truth."""
from __future__ import annotations

import json
from app.core.demo_trace import emit, observe
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Literal, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.gates.answer_router import CLARIFY_TMPL, FACT_CUES, asked_atom_kinds
from app.ai.gates.answer_router import route_query
from app.ai.gates.entity_index import KINSHIP, PLACE_SUFFIXES, build_room_entity_index
from app.ai.gates.output_gate import validate_plan_selection
from app.ai.gates.textrules import content_tokens, fold_family_terms, normalize_query, num_atoms, time_atoms
from app.ai.model_registry import get_model_profile
from app.ai.record_candidate_tool import CandidateSelection
from app.ai.providers.base import InferenceRequest, InferenceProvider
from app.models.auth_identity import require_persisted_principal
from app.models.family import FamilyMember
from app.models.processing_job import ProcessingJob
from app.models.retrieval import RecordChunk
from app.retrieval.chunking import (
    RetrievalChunk, canonical_json, sha256_text,
)
from app.schemas.ai import AskRequest, AskResponse, OriginalAskResponse, SecureAskResponse
from app.services.retrieval import RetrievalSearchNotFound, search_room
from app.retrieval.admission import admit_candidates, safety_related
from app.ai.gates.record_discovery import discovery_query, discovery_candidates, discovery_topic, distinct_records

_FACT_NAMESPACE = uuid.UUID("f8f41d4d-4db1-4b13-a9a6-f55bcb651ee2")
_DATE_RE = re.compile(r"\d{4}\s*년(?:\s*\d{1,2}\s*월)?(?:\s*\d{1,2}\s*일)?|\d{1,2}\s*월\s*\d{1,2}\s*일")
_EVENTS = ("검진", "생일", "여행", "방문", "예약", "입학", "졸업", "결혼", "산책")
_RELATIONS = ("함께", "에게", "께서", "와", "과")


@dataclass(frozen=True)
class QueryFrame:
    raw_query: str
    normalized_query: str
    entities: tuple[str, ...]
    relations: tuple[str, ...]
    events: tuple[str, ...]
    dates: tuple[str, ...]
    times: tuple[str, ...]
    numbers: tuple[str, ...]
    predicate: str | Literal["unknown"]
    polarity: Literal["positive", "negative", "unknown"]
    tense: Literal["past", "present", "future", "unknown"]
    modality: Literal["assertive", "question", "possibility", "unknown"]
    exact_terms: tuple[str, ...]
    semantic_intent: Literal["fact", "decision", "recollection", "creative", "unknown"]
    asked_kinds: tuple[str, ...]


@dataclass(frozen=True)
class SourceRef:
    room_id: str
    chunk_id: str
    record_id: str
    file_id: str
    processing_job_id: str
    source_attempt: int
    source_result_sha256: str
    text_sha256: str
    chunk_char_start: int
    chunk_char_end: int
    source_locator_json: str
    extractor: str
    extractor_id: str
    review_status: str
    user_confirmed: bool


@dataclass(frozen=True)
class LiteralFact:
    fact_id: str
    source: SourceRef
    quote: str
    quote_char_start: int
    quote_char_end: int
    labels: tuple[str, ...]


@dataclass(frozen=True)
class RecordFactSheet:
    facts: tuple[LiteralFact, ...]


@dataclass(frozen=True)
class ApprovedClaim:
    claim_id: str
    fact_id: str
    claim_version: int = 1
    active: bool = True
    superseded_by: str | None = None


@dataclass(frozen=True)
class AnswerPlan:
    route: Literal["ANSWER"]
    claims: tuple[ApprovedClaim, ...]
    facts: tuple[LiteralFact, ...]


def build_query_frame(query: str) -> QueryFrame:
    normalized = normalize_query(query)
    folded = fold_family_terms(normalized)
    terms = content_tokens(folded)
    entities = sorted({term for term in KINSHIP if term in folded} | {
        term for term in terms if any(term.endswith(suffix) for suffix in PLACE_SUFFIXES)
    })
    events = tuple(word for word in _EVENTS if word in folded)
    relations = tuple(word for word in _RELATIONS if word in folded) or ("unknown",)
    negative = bool(re.search(r"(?<!\S)(?:안|못)(?=\s)|지\s*(?:않|못)|없(?:다|어|었|는)", normalized))
    polarity = "negative" if negative else ("positive" if re.search(r"(?:있|했|였다|이다)", normalized) else "unknown")
    tense = "past" if re.search(r"(?:했|였|었|았다|었다)", normalized) else (
        "future" if re.search(r"(?:할\s+예정|예정|겠(?:다|어|습니까))", normalized) else "unknown"
    )
    modality = "question" if re.search(r"[?？]|(?:언제|몇|어디|누구|누가|무엇)", normalized) else (
        "possibility" if re.search(r"(?:수 있|지도 모르|아마)", normalized) else "unknown"
    )
    intent = "creative" if re.search(r"(?:편지|시를|소설|써줘)", normalized) else (
        "recollection" if "기억" in normalized else ("fact" if any(cue in normalized for cue in FACT_CUES) else "unknown")
    )
    if "decision" in asked_atom_kinds(normalized):
        intent = "decision"
    return QueryFrame(
        raw_query=query, normalized_query=normalized, entities=tuple(entities),
        relations=relations, events=events or ("unknown",),
        dates=tuple(match.group() for match in _DATE_RE.finditer(normalized)),
        times=tuple(surface for surface, _ in time_atoms(normalized)),
        numbers=tuple(sorted(num_atoms(normalized))), predicate=events[0] if events else "unknown",
        polarity=polarity, tense=tense, modality=modality,
        exact_terms=tuple(sorted(terms)), semantic_intent=intent,
        asked_kinds=tuple(sorted(asked_atom_kinds(normalized))),
    )


def build_fact_sheet(chunks: Sequence[RetrievalChunk]) -> RecordFactSheet:
    facts = []
    for chunk in chunks:
        if not isinstance(chunk, RetrievalChunk) or not all((
            chunk.id, chunk.room_id, chunk.record_id, chunk.file_id, chunk.processing_job_id,
            chunk.source_result_sha256, chunk.text_sha256, chunk.source_locator_json, chunk.text,
        )) or chunk.source_attempt < 0 or chunk.char_start < 0 or chunk.char_end <= chunk.char_start:
            raise ValueError("Invalid retrieval chunk identity or span")
        source = SourceRef(
            room_id=chunk.room_id, chunk_id=chunk.id, record_id=chunk.record_id,
            file_id=chunk.file_id, processing_job_id=chunk.processing_job_id,
            source_attempt=chunk.source_attempt, source_result_sha256=chunk.source_result_sha256,
            text_sha256=chunk.text_sha256, chunk_char_start=chunk.char_start,
            chunk_char_end=chunk.char_end, source_locator_json=chunk.source_locator_json,
            extractor=chunk.extractor, extractor_id=chunk.extractor_id,
            review_status=chunk.review_status, user_confirmed=chunk.user_confirmed,
        )
        fact_id = str(uuid.uuid5(_FACT_NAMESPACE, canonical_json({
            "source": asdict(source), "quote_char_start": 0, "quote_char_end": len(chunk.text),
        })))
        labels = tuple(sorted({kind for kind in ("time", "number") if
                               (time_atoms(chunk.text) if kind == "time" else num_atoms(chunk.text))}))
        facts.append(LiteralFact(fact_id, source, chunk.text, 0, len(chunk.text), labels))
    return RecordFactSheet(tuple(facts))


def build_answer_plan(sheet: RecordFactSheet, *, k: int) -> AnswerPlan:
    if k < 1 or len({fact.fact_id for fact in sheet.facts[:k]}) != len(sheet.facts[:k]):
        raise ValueError("Invalid plan size or duplicate facts")
    facts = sheet.facts[:k]
    return AnswerPlan("ANSWER", tuple(ApprovedClaim(f"claim-{i}", fact.fact_id,
                                      claim_version=fact.source.source_attempt + 1)
                                      for i, fact in enumerate(facts, 1)), facts)


def _public_locator(raw: str) -> dict:
    try:
        locator = json.loads(raw)
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ValueError("Invalid source locator") from exc

    def index(value: object) -> bool:
        return type(value) is int and value >= 0

    def seconds(value: object) -> bool:
        if value is None:
            return True  # M2 emits None when a segment has no timestamp.
        try:
            return type(value) in (int, float) and value >= 0 and math.isfinite(value)
        except OverflowError:
            return False

    valid = False
    if isinstance(locator, dict):
        kind = locator.get("kind")
        if kind == "full_text":
            valid = set(locator) == {"kind"}
        elif kind == "multimodal":
            from app.schemas.multimodal import MediaLocator
            try:
                MediaLocator.model_validate(locator)
                valid = True
            except ValueError:
                valid = False
        elif kind == "ocr_regions":
            indexes = locator.get("indexes")
            valid = (set(locator) == {"kind", "indexes"} and isinstance(indexes, list)
                     and bool(indexes) and all(index(item) for item in indexes))
        elif kind == "stt_segments":
            segments = locator.get("segments")
            if set(locator) == {"kind", "segments"} and isinstance(segments, list) and segments:
                valid = all(
                    isinstance(item, dict)
                    and "index" in item
                    and set(item) <= {"index", "start_seconds", "end_seconds"}
                    and index(item["index"])
                    and seconds(item.get("start_seconds"))
                    and seconds(item.get("end_seconds"))
                    and (item.get("start_seconds") is None or item.get("end_seconds") is None
                         or item["start_seconds"] <= item["end_seconds"])
                    for item in segments
                )
    if not valid:
        raise ValueError("Invalid source locator")
    return locator


def render_plan(plan: AnswerPlan, claim_ids: Sequence[str]) -> tuple[str, list[dict]]:
    expected = {claim.claim_id for claim in plan.claims}
    if (not claim_ids or len(claim_ids) != len(expected)
            or not all(isinstance(claim_id, str) for claim_id in claim_ids)
            or set(claim_ids) != expected):
        raise ValueError("Invalid claim selection")
    facts = {fact.fact_id: fact for fact in plan.facts}
    if any(not claim.active or claim.superseded_by is not None
           or claim.claim_version != facts[claim.fact_id].source.source_attempt + 1
           for claim in plan.claims):
        raise GroundedEvidenceChanged("STALE_SOURCE")
    claims = {claim.claim_id: claim.fact_id for claim in plan.claims}
    quotes = []
    citations = []
    for claim_id in claim_ids:
        fact = facts[claims[claim_id]]
        locator = _public_locator(fact.source.source_locator_json)
        quotes.append(fact.quote)
        citations.append({
            "fact_id": fact.fact_id, "chunk_id": fact.source.chunk_id,
            "record_id": fact.source.record_id, "file_id": fact.source.file_id,
            "chunk_char_start": fact.source.chunk_char_start,
            "chunk_char_end": fact.source.chunk_char_end,
            "source_locator": locator, "extractor": fact.source.extractor,
            "extractor_id": fact.source.extractor_id,
            "review_status": fact.source.review_status,
            "user_confirmed": fact.source.user_confirmed,
            "evidence_status": "extracted",
        })
    return "기록에서 추출된 내용\n" + "\n".join(quotes), citations


class GroundedEvidenceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class GroundedAnswerSnapshot:
    frame: QueryFrame
    model_id: str
    route: str
    deterministic_answer: str | None
    facts: RecordFactSheet
    plan: AnswerPlan | None
    catalogue_evidence: object | None = None
    original_digests: tuple[tuple[str, str], ...] = ()
    safety_reason: str | None = None
    record_discovery: bool = False
    event_memory: tuple[dict, ...] = ()
    candidate_selection: CandidateSelection | None = None


@dataclass(frozen=True)
class GroundedAnswerResult:
    snapshot: GroundedAnswerSnapshot
    claim_ids: tuple[str, ...]
    provider_mode: str


def _require_member(db: Session, room_id: str, user_id: str) -> None:
    require_persisted_principal(db, user_id)
    if db.scalar(select(FamilyMember.id).where(
        FamilyMember.room_id == room_id, FamilyMember.user_id == user_id,
        FamilyMember.is_active.is_(True),
    ).limit(1)) is None:
        raise RetrievalSearchNotFound("Retrieval room not found")


# Bound admitted answering and veto evidence, independently of corpus size/display k.
SAFETY_CANDIDATE_DEPTH = 50
SAFETY_CANDIDATE_FETCH_LIMIT = SAFETY_CANDIDATE_DEPTH + 1
SAFETY_CANDIDATE_LIMIT_EXCEEDED = "SAFETY_CANDIDATE_LIMIT_EXCEEDED"


def _safety_limit_snapshot(frame, model_id, candidate_count, catalogue_evidence=None):
    if candidate_count <= SAFETY_CANDIDATE_DEPTH:
        return None
    emit("ANSWER ROUTER", decision="CLARIFY", admitted_evidence=0)
    return GroundedAnswerSnapshot(
        frame, model_id, "CLARIFY", CLARIFY_TMPL, RecordFactSheet(()), None,
        catalogue_evidence, safety_reason=SAFETY_CANDIDATE_LIMIT_EXCEEDED,
    )


def _audio_fact_query(query: str) -> str | None:
    pattern = r"(?<![가-힣])(?:음성|오디오|녹음)(?:\s+기록)?(?:에서|에|의|을|를)?(?=\s|$)"
    return re.sub(pattern, " ", query).strip() if re.search(pattern, query) else None


def _audio_chunk(chunk: RetrievalChunk) -> bool:
    locator = json.loads(chunk.source_locator_json)
    return locator.get("media_type") == "AUDIO" or locator.get("kind") == "stt_segments"


def _safety_veto(decision, frame, chunks, entity_index, *, audio_only=False):
    """Unranked/current evidence may veto an answer, never authorize or be cited.

    Retain pre-existing conflict safety even for missing embeddings and Korean
    lexical inflection misses. Explicitly unrelated people/events cannot veto.
    """
    if decision.route == "ANSWER":
        safety = route_query(frame.normalized_query,
            [c.text for c in chunks if safety_related(frame.normalized_query, c.text, entity_index)],
            entity_index)
        if audio_only and safety.route != "CONFLICT" and safety.detail != "descriptive_conflict":
            safety = route_query(frame.normalized_query,
                [c.text for c in chunks if _audio_chunk(c)
                 and safety_related(frame.normalized_query, c.text, entity_index)], entity_index)
        if safety.route == "CONFLICT" or (safety.route == "CLARIFY" and
                safety.detail in {"ambiguous_person", "ambiguous_event", "descriptive_conflict"}):
            return safety
    return decision


def load_grounded_answer_snapshot(
    *, session_factory: Callable[[], Session], room_id: str, user_id: str,
    payload: AskRequest, retrieval_provider: object, candidate_limit: int,
) -> GroundedAnswerSnapshot:
    from app.ai.demo_service import load_room_entity_sources

    get_model_profile(payload.model_id)
    db = session_factory()
    try:
        _require_member(db, room_id, user_id)
    finally:
        db.close()

    discovery = discovery_query(payload.query)
    safety_candidates = []
    hits = search_room(
        room_id=room_id, user_id=user_id, query=discovery or _audio_fact_query(payload.query) or payload.query, arm=payload.arm,
        k=SAFETY_CANDIDATE_FETCH_LIMIT, provider=retrieval_provider, session_factory=session_factory,
        candidate_limit=max(candidate_limit, SAFETY_CANDIDATE_FETCH_LIMIT),
        safety_candidates=safety_candidates,
    )
    frame = build_query_frame(payload.query)
    db = session_factory()
    try:
        _require_member(db, room_id, user_id)
        from app.services.record_policy import allowed_record_ids
        authorized = set(allowed_record_ids(db, user_id, room_id, purpose="rag"))
        if any(c.record_id not in authorized for c in safety_candidates):
            raise GroundedEvidenceChanged("Evidence changed")
        chunks = []
        for candidate in safety_candidates:
            row = db.scalar(select(RecordChunk).where(
                RecordChunk.id == candidate.id, RecordChunk.room_id == room_id,
                RecordChunk.record_id.in_(authorized),
            ))
            if row is None:
                raise GroundedEvidenceChanged("Evidence changed")
            chunks.append(RetrievalChunk(**{
                key: getattr(row, key) for key in RetrievalChunk.__dataclass_fields__
            }))
            if chunks[-1] != candidate:
                raise GroundedEvidenceChanged("Evidence changed")
        sheet = build_fact_sheet(chunks)
        if any(fact.source.record_id not in authorized or not _source_is_current(db, fact, room_id)
               for fact in sheet.facts):
            raise GroundedEvidenceChanged("Original evidence unavailable; reprocess current file")
        digests = tuple((fact.source.chunk_id, json.loads(
            db.get(ProcessingJob, fact.source.processing_job_id).result_json)["source_sha256"])
            for fact in sheet.facts)
        member_names, record_texts = load_room_entity_sources(db, room_id, user_id=user_id)
    finally:
        db.close()

    entity_index = build_room_entity_index(member_names, record_texts)
    if discovery is not None:
        hits = discovery_candidates(hits, discovery, entity_index)
        limited = _safety_limit_snapshot(frame, payload.model_id, len(hits))
        if limited is not None:
            return limited
        plan = build_answer_plan(build_fact_sheet([h.chunk for h in distinct_records(hits)]), k=payload.k) if hits else None
        return GroundedAnswerSnapshot(frame, payload.model_id, "ANSWER" if plan else "NO_RECORD",
            None if plan else "접근 가능한 관련 기록을 찾지 못했습니다.", sheet, plan,
            original_digests=digests, record_discovery=True)
    audio_query = _audio_fact_query(payload.query)
    if audio_query is not None:
        frame = replace(build_query_frame(audio_query), raw_query=payload.query)
        hits = [hit for hit in hits if _audio_chunk(hit.chunk)]
    hits = admit_candidates(hits, frame.normalized_query, entity_index)
    # Complete ranked admission precedes the limit: a top-k cutoff could hide
    # relevant overflow or a contradiction. Unranked veto evidence shares the
    # bound, but is never promoted into answer-authorizing hits.
    admitted_ids = {hit.chunk.id for hit in hits}
    chunks = tuple(c for c in chunks if c.id in admitted_ids or
                   safety_related(frame.normalized_query, c.text, entity_index))
    limited = _safety_limit_snapshot(frame, payload.model_id, len(chunks))
    if limited is not None:
        return limited
    # Named places must come from the evidence being evaluated, not unrelated
    # room metadata. Keep the complete person index for subject disambiguation.
    entity_index = replace(entity_index, places=set())
    decision = route_query(frame.normalized_query, [hit.chunk.text for hit in hits],
                           entity_index, relevance_admitted=True)
    decision = _safety_veto(decision, frame, chunks, entity_index, audio_only=audio_query is not None)
    current_chunks = {chunk.id: chunk for chunk in chunks}
    answer_sheet = build_fact_sheet([current_chunks[hit.chunk.id] for hit in hits
        if route_query(frame.normalized_query, [hit.chunk.text], entity_index,
                       relevance_admitted=True).route == "ANSWER"])
    if decision.route == "ANSWER" and not answer_sheet.facts:
        # Atoms spread over individually insufficient records cannot authorize a claim.
        decision = route_query(frame.normalized_query, [hits[0].chunk.text],
                               entity_index, relevance_admitted=True)
    plan = build_answer_plan(answer_sheet, k=payload.k) if decision.route == "ANSWER" else None
    emit("ANSWER ROUTER", decision=decision.route, admitted_evidence=len(hits))
    return GroundedAnswerSnapshot(frame, payload.model_id, decision.route, decision.answer, sheet, plan,
                                  original_digests=digests)


def generate_grounded_answer(
    snapshot: GroundedAnswerSnapshot, *, provider_factory: Callable[[str], InferenceProvider],
) -> GroundedAnswerResult:
    from app.ai.prompt_builder import build_plan_selection_prompt

    if snapshot.plan is None:
        emit("LLM INFERENCE", invoked=False)
        emit("CLAIM VALIDATION", validation="SKIPPED")
        return GroundedAnswerResult(snapshot, (), "not_called")
    fallback = tuple(claim.claim_id for claim in snapshot.plan.claims)
    if snapshot.record_discovery:
        emit("LLM INFERENCE", invoked=False)
        return GroundedAnswerResult(snapshot, fallback, "not_called")
    try:
        request = InferenceRequest(
            model_id=snapshot.model_id, user_query=snapshot.frame.raw_query,
            prompt_package=build_plan_selection_prompt(
                model_id=snapshot.model_id, query=snapshot.frame.raw_query, plan=snapshot.plan,
            ),
        )
        response = _demo_inference(provider_factory(snapshot.model_id), request)
        selected = validate_plan_selection(response.output_text, snapshot.plan)
    except Exception:
        selected = None
    emit("CLAIM VALIDATION", validation="PASS" if selected else "FAIL")
    if not selected and snapshot.catalogue_evidence is not None:
        from app.services.secure_runtime import SecureUnavailable
        raise SecureUnavailable() from None
    return GroundedAnswerResult(snapshot, selected or fallback, "selected" if selected else "fallback")


def _source_is_current(db: Session, fact: LiteralFact, room_id: str) -> bool:
    source = fact.source
    row = db.scalar(select(RecordChunk).where(
        RecordChunk.id == source.chunk_id, RecordChunk.room_id == room_id,
    ))
    if row is None:
        return False
    try:
        stored_chunk = RetrievalChunk(**{
            key: getattr(row, key) for key in RetrievalChunk.__dataclass_fields__
        })
        current = build_fact_sheet([stored_chunk]).facts[0]
    except ValueError:
        return False
    if (current.source != source or current.quote != fact.quote
            or sha256_text(row.text) != row.text_sha256):
        return False

    from app.services.retrieval import chunk_is_current

    if not chunk_is_current(db, stored_chunk, room_id):
        return False
    digest = json.loads(db.get(ProcessingJob, source.processing_job_id).result_json).get("source_sha256")
    return isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None


@observe("CITATION VALIDATION", lambda result, a, kw: dict(citation_count=len(result.citations), validation="PASS" if result.citations else "SKIPPED"))
def revalidate_and_render_grounded_answer(
    *, session_factory: Callable[[], Session], room_id: str, user_id: str,
    result: GroundedAnswerResult,
) -> AskResponse:
    snapshot = result.snapshot
    db = session_factory()
    try:
        _require_member(db, room_id, user_id)
        facts = snapshot.facts.facts
        from app.services.record_policy import allowed_record_ids
        authorized = set(allowed_record_ids(db, user_id, room_id, purpose="rag"))
        if any(fact.source.record_id not in authorized or not _source_is_current(db, fact, room_id)
               for fact in facts):
            raise GroundedEvidenceChanged("Evidence changed")
    finally:
        db.close()

    if snapshot.plan is None:
        return OriginalAskResponse(gate_route=snapshot.route, answer=snapshot.deterministic_answer,
                           citations=[], gate_action="llm_not_called", provider_mode="not_called")
    try:
        answer, citations = render_plan(snapshot.plan, result.claim_ids)
        if snapshot.record_discovery:
            answer = answer.replace("기록에서 추출된 내용", "관련 기록의 원문 발췌", 1)
    except ValueError as error:
        raise GroundedEvidenceChanged("Evidence changed") from error
    digests = dict(snapshot.original_digests)
    for citation in citations:
        digest = digests[citation["chunk_id"]]
        citation["source_sha256"] = digest
        citation["original_url"] = (f'/api/v1/uploads/{citation["record_id"]}/files/'
                                    f'{citation["file_id"]}/content?expected_sha256={digest}')
    return OriginalAskResponse(gate_route="ANSWER", answer=answer, citations=citations,
                       gate_action="record_discovery_render" if snapshot.record_discovery else "literal_render",
                       provider_mode=result.provider_mode)


class SyntheticPlanProvider:
    """Inert B-only selection transport, explicitly injected; never chosen at startup."""
    def generate(self, request):
        from app.ai.providers.base import InferenceResponse
        selected = [line.split(" | ", 1)[0] for line in request.prompt_package["retrieved_evidence"]]
        return InferenceResponse("synthetic-plan", request.model_id, json.dumps({"claim_ids": selected}), "synthetic")


def secure_inference_factory(runtime):
    provider = getattr(runtime, "inference_provider", None)
    from app.services.secure_local_providers import LocalInference
    local = getattr(runtime, "local_provider_admission", False) is True and type(provider) is LocalInference
    if provider is not None and type(provider) is not SyntheticPlanProvider and not local:
        from app.services.secure_runtime import SecureUnavailable
        raise SecureUnavailable()
    def select(_model_id):
        if provider is None:
            raise RuntimeError("Provider unavailable")
        return provider
    return select


def load_secure_grounded_snapshot(*, runtime, user_id, room_id, payload, candidate_limit, timings=None, query_person_names=(), discovery_record_ids=None):
    from app.services.retrieval import search_secure_room
    from app.ai.gates.record_discovery import audio_discovery
    from app.retrieval.ranking import RankedChunk
    get_model_profile(payload.model_id)
    discovery = discovery_query(payload.query)
    retrieval_query = discovery_topic(discovery, query_person_names) if discovery is not None and discovery_record_ids is not None else discovery or _audio_fact_query(payload.query) or payload.query
    audio_topic = discovery_topic(discovery, query_person_names) if discovery is not None and audio_discovery(discovery) else None
    if audio_topic is not None:
        retrieval_query = audio_topic or discovery
    hits, corpus = search_secure_room(runtime=runtime, user_id=user_id, room_id=room_id, query=retrieval_query,
        arm=payload.arm, k=SAFETY_CANDIDATE_FETCH_LIMIT,
        candidate_limit=max(candidate_limit, SAFETY_CANDIDATE_FETCH_LIMIT), timings=timings,
        for_answering=True)
    frame = build_query_frame(payload.query)
    chunks = corpus.chunks
    entity_index = build_room_entity_index((*corpus.member_names, *query_person_names), corpus.record_texts)
    if discovery is not None:
        if audio_topic == "":
            hits = [RankedChunk(chunk, 1.0, arms=frozenset({"metadata"})) for chunk in corpus.chunks]
        if discovery_record_ids is not None:
            hits = [h for h in hits if h.chunk.record_id in discovery_record_ids]
        hits = discovery_candidates(hits, discovery, entity_index, subject_bound=discovery_record_ids is not None)
        limited = _safety_limit_snapshot(frame, payload.model_id, len(hits), corpus)
        if limited is not None:
            return limited
        plan = build_answer_plan(build_fact_sheet([h.chunk for h in distinct_records(hits)]), k=payload.k) if hits else None
        return GroundedAnswerSnapshot(frame, payload.model_id, "ANSWER" if plan else "NO_RECORD",
            None if plan else "접근 가능한 관련 기록을 찾지 못했습니다.", build_fact_sheet(corpus.chunks),
            plan, corpus, record_discovery=True)
    audio_query = _audio_fact_query(payload.query)
    if audio_query is not None:
        frame = replace(build_query_frame(audio_query), raw_query=payload.query)
        hits = [hit for hit in hits if _audio_chunk(hit.chunk)]
    hits = admit_candidates(hits, frame.normalized_query, entity_index)
    # Complete ranked admission precedes the limit: a top-k cutoff could hide
    # relevant overflow or a contradiction. Unranked veto evidence shares the
    # bound, but is never promoted into answer-authorizing hits.
    admitted_ids = {hit.chunk.id for hit in hits}
    chunks = tuple(c for c in chunks if c.id in admitted_ids or
                   safety_related(frame.normalized_query, c.text, entity_index))
    limited = _safety_limit_snapshot(frame, payload.model_id, len(chunks), corpus)
    if limited is not None:
        return limited
    # Named places must come from the evidence being evaluated, not unrelated
    # room metadata. Keep the complete person index for subject disambiguation.
    entity_index = replace(entity_index, places=set())
    decision = route_query(frame.normalized_query, [hit.chunk.text for hit in hits],
                           entity_index, relevance_admitted=True)
    decision = _safety_veto(decision, frame, chunks, entity_index, audio_only=audio_query is not None)
    # Retain the complete provenance sheet for publication-time validation only.
    sheet = build_fact_sheet(corpus.chunks)
    answer_sheet = build_fact_sheet([hit.chunk for hit in hits
        if route_query(frame.normalized_query, [hit.chunk.text], entity_index,
                       relevance_admitted=True).route == "ANSWER"])
    if decision.route == "ANSWER" and not answer_sheet.facts:
        # Atoms spread over individually insufficient records cannot authorize a claim.
        decision = route_query(frame.normalized_query, [hits[0].chunk.text],
                               entity_index, relevance_admitted=True)
    plan = build_answer_plan(answer_sheet, k=payload.k) if decision.route == "ANSWER" else None
    emit("ANSWER ROUTER", decision=decision.route, admitted_evidence=len(hits))
    return GroundedAnswerSnapshot(frame, payload.model_id, decision.route, decision.answer, sheet, plan, corpus)


@observe("CITATION VALIDATION", lambda result, a, kw: dict(citation_count=len(result.citations), validation="PASS" if result.citations else "SKIPPED"))
def render_secure_grounded_answer(runtime, result):
    from app.services.secure_retrieval import revalidate_evidence
    from app.api.routes.secure_media import original_url
    from app.api.routes.secure_operations import tx
    from app.services import secure_catalogue as cat
    snapshot = result.snapshot
    try:
        corpus = snapshot.catalogue_evidence
        if corpus is None:
            raise GroundedEvidenceChanged()
        revalidate_evidence(runtime, corpus)
        current = {f.fact_id: f for f in build_fact_sheet(corpus.chunks).facts}
        if any(current.get(f.fact_id) != f for f in snapshot.facts.facts):
            raise GroundedEvidenceChanged()
        if snapshot.plan is None:
            return SecureAskResponse(gate_route=snapshot.route, answer=snapshot.deterministic_answer,
                citations=[], gate_action="llm_not_called", provider_mode="not_called")
        if result.provider_mode != "selected" and not (snapshot.record_discovery and result.provider_mode == "not_called"):
            from app.services.secure_runtime import SecureUnavailable
            raise SecureUnavailable()
        if any(current.get(f.fact_id) != f for f in snapshot.plan.facts):
            raise GroundedEvidenceChanged()
        answer, citations = render_plan(snapshot.plan, result.claim_ids)
        if snapshot.record_discovery:
            answer = answer.replace("기록에서 추출된 내용", "관련 기록의 원문 발췌", 1)
        records = {source[1]["id"]: source for source in corpus.records}
        for citation in citations:
            source = records.get(citation["record_id"])
            if source is None or source[3] is None or ("file", uuid.UUID(citation["file_id"])) not in tuple(p[:2] for p in source[0].pointers):
                raise GroundedEvidenceChanged()
            identity, _, _, receipt = source[3]
            from app.services.record_policy import require_record_access
            with tx() as db:
                policy = require_record_access(db, str(corpus.catalogue.principal_id), citation["record_id"], purpose="rag")
            citation.update(source_object_id=str(identity.object_id), source_object_version=identity.object_version,
                source_ciphertext_sha256=receipt.ciphertext_sha256.hex(),
                original_url=original_url(citation["record_id"], citation["file_id"], identity, receipt, citation=dict(
                    processing_job_id=current[citation["fact_id"]].source.processing_job_id,
                    authorization_epoch=policy["authorization_epoch"],
                    source_attempt=current[citation["fact_id"]].source.source_attempt,
                    source_result_sha256=current[citation["fact_id"]].source.source_result_sha256)))
        return SecureAskResponse(gate_route="ANSWER", answer=answer, citations=citations,
            gate_action="record_discovery_render" if snapshot.record_discovery else "literal_render",
            provider_mode=result.provider_mode)
    except (cat.SecureStateChanged, ValueError, TypeError):
        raise GroundedEvidenceChanged("Evidence changed") from None


@observe("LLM INFERENCE", lambda result, a, kw: dict(invoked=True))
def _demo_inference(provider, request):
    emit("LLM INFERENCE", invoked=True,
         model="gemma4:e2b" if type(provider).__name__ == "LocalInference" else
               "synthetic" if type(provider).__name__ == "SyntheticPlanProvider" else "other")
    return provider.generate(request)
