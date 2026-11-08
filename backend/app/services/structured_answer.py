"""Structured computation with existing exact source/citation verification.

No embedding, generative model, VLM or STT invocation. A missing current indexed
source blocks the entire answer rather than silently changing its aggregation.
"""

import json
from time import perf_counter
from typing import Literal, cast

from sqlalchemy import select

from app.ai import grounded_answering as grounded
from app.models.processing_job import ProcessingJob
from app.models.retrieval import RecordChunk
from app.retrieval.chunking import RetrievalChunk
from app.schemas.ai import OriginalAskResponse, SecureAskResponse
from app.schemas.structured_query import StructuredResult
from app.services import family_context, secure_family_context


def _same(a, b):
    def value(x):
        return (
            x.model_dump(exclude={"planner_ms", "execution_ms", "rendering_ms"})
            if x
            else None
        )

    return value(a) == value(b)


def _text(result):
    if result.route != "ANSWER":
        return (
            "조건이나 시점을 더 구체적으로 알려 주세요."
            if result.route == "CLARIFY"
            else "확인할 수 있는 기록이 없습니다."
        )
    if result.count is not None:
        groups = "; ".join(
            f"{result.member_labels.get(member, member)}: {count}회"
            for member, count in result.grouped_counts.items()
        )
        return "접근 가능한 검토된 사건 기준: " + (groups or f"{result.count}회")
    rows = []
    for e in result.entries:
        t = e.occurred_at
        when = t.value or f"{t.start}~{t.end}"
        rows.append(
            f"{when} ({t.precision}) {e.title}"
            + (f" — {e.location['label']}" if e.location.get("label") else "")
            + ("; 명시적 불참 근거 확인" if result.plan.excluded_member_ids else "")
            + (
                (
                    "; 여행으로 축하"
                    if e.travel_celebration
                    else "; 여행이 아닌 축하로 명시됨"
                )
                if e.travel_celebration is not None
                else ""
            )
        )
        if result.plan.include_participants:
            onsite = [
                result.member_labels.get(member, member)
                for member, modes in e.member_modes.items()
                if "ONSITE" in modes
            ]
            rows.append("명시적으로 확인된 현장 참여자: " + ", ".join(onsite))
    if result.plan.compare_gift_use:

        def format_time(value):
            return (value.value or f"{value.start}~{value.end}") if value else "UNKNOWN"

        rows.append(
            f"received_at: {format_time(result.received_at)}; "
            f"first_used_at: {format_time(result.first_used_at)}; "
            f"use_confirmed_at: {format_time(result.use_confirmed_at)}"
        )
        if result.first_used_at is None:
            rows.append(
                "최초 사용일은 확인 가능한 기록에 명시되지 않았습니다. 꺼낸 날이나 사용 확인일을 최초 사용일로 추정하지 않습니다."
            )
    return "\n".join(rows)


def _snapshot(payload, result, chunks, corpus=None, digests=()):
    ids = {s.record_id for e in result.entries for s in e.sources}
    selected = sorted(
        (c for c in chunks if c.record_id in ids), key=lambda c: (c.record_id, c.id)
    )
    # Keep all supporting sources, independent of semantic ranking or user k.
    if not ids or {c.record_id for c in selected} != ids:
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    sheet = grounded.build_fact_sheet(selected)
    plan = grounded.build_answer_plan(sheet, k=len(sheet.facts))
    snapshot = grounded.GroundedAnswerSnapshot(
        grounded.build_query_frame(payload.query),
        payload.model_id,
        "ANSWER",
        None,
        sheet,
        plan,
        corpus,
        digests,
    )
    return grounded.GroundedAnswerResult(
        snapshot, tuple(c.claim_id for c in plan.claims), "selected"
    )


def _empty(response_type, result):
    route = "NO_RECORD" if result.route == "UNSUPPORTED" else result.route
    return response_type(
        gate_route=route,
        answer=_text(result),
        citations=[],
        gate_action="llm_not_called",
        provider_mode="not_called",
        structured_result=result,
    )


def _response_route(result: StructuredResult) -> Literal["ANSWER", "NEEDS_DATA"]:
    if result.reason == "FIRST_USE_NOT_RECORDED":
        return "NEEDS_DATA"
    return "ANSWER"


def secure_answer(runtime, uid, room, payload):
    from app.services.secure_retrieval import materialize_room

    def evaluate():
        return secure_family_context.query(
            runtime,
            uid,
            room,
            "structured_ask_query",
            payload.query,
            filters=payload.structured_query,
            as_of=payload.as_of,
        )

    result = cast(StructuredResult | None, evaluate())
    if result is None:
        return None
    if result.route != "ANSWER":
        return _empty(SecureAskResponse, result)
    corpus = materialize_room(runtime, uid, room)
    render_started = perf_counter()
    answer = grounded.render_secure_grounded_answer(
        runtime, _snapshot(payload, result, corpus.chunks, corpus)
    )
    if not _same(result, evaluate()):
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    result.rendering_ms = (perf_counter() - render_started) * 1000
    return answer.model_copy(
        update={
            "gate_route": _response_route(result),
            "answer": _text(result),
            "provider_mode": "not_called",
            "gate_action": "structured_render",
            "structured_result": result,
        }
    )


def legacy_answer(factory, uid, room, payload):
    def evaluate():
        with factory() as db:
            return family_context.structured_ask_query(
                db, uid, room, payload.query, payload.structured_query, payload.as_of
            )

    result = evaluate()
    if result is None:
        return None
    if result.route != "ANSWER":
        return _empty(OriginalAskResponse, result)
    ids = {s.record_id for e in result.entries for s in e.sources}
    with factory() as db:
        from app.services.record_policy import allowed_record_ids

        family_context.membership(db, uid, room)
        allowed = allowed_record_ids(db, uid, room, purpose="rag")
        if not ids <= set(allowed):
            raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
        chunks = [
            RetrievalChunk(
                **{
                    key: getattr(row, key)
                    for key in RetrievalChunk.__dataclass_fields__
                }
            )
            for row in db.scalars(
                select(RecordChunk).where(
                    RecordChunk.room_id == room,
                    RecordChunk.record_id.in_(ids),
                    RecordChunk.record_id.in_(allowed),
                )
            )
        ]
        sheet = grounded.build_fact_sheet(chunks)
        if any(not grounded._source_is_current(db, fact, room) for fact in sheet.facts):
            raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
        digests = tuple(
            (
                f.source.chunk_id,
                json.loads(
                    db.get(ProcessingJob, f.source.processing_job_id).result_json
                )["source_sha256"],
            )
            for f in sheet.facts
        )
    render_started = perf_counter()
    answer = grounded.revalidate_and_render_grounded_answer(
        session_factory=factory,
        room_id=room,
        user_id=uid,
        result=_snapshot(payload, result, chunks, digests=digests),
    )
    if not _same(result, evaluate()):
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    result.rendering_ms = (perf_counter() - render_started) * 1000
    return answer.model_copy(
        update={
            "gate_route": _response_route(result),
            "answer": _text(result),
            "provider_mode": "not_called",
            "gate_action": "structured_render",
            "structured_result": result,
        }
    )
