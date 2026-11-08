"""Bounded M5.2 evidence composition. Participation, memory and emotion stay distinct.

Only reviewed typed statements establish mental states. Neither the planner nor
renderer calls a model. The M3 projection admits ACL-approved components first.
"""

import re
from itertools import combinations
from time import perf_counter
from types import SimpleNamespace
from typing import Literal, cast
from urllib.parse import urlencode

from sqlalchemy import select

from app.ai import grounded_answering as grounded
from app.models.family import FamilyMember
from app.models.family_context import Event, EventParticipation
from app.schemas.ai import SecureAskResponse
from app.schemas.claims import ClaimQuery
from app.services import secure_family_context as context
from app.services.family_context import AskQueryContext

OPERATIONS = {"MEMORY_STATE", "PARTICIPATION_MEMORY_COMPARE", "MENTAL_STATE_QUERY"}
EMOTIONS = {
    "AFRAID": r"무서(?:웠|워|운|움)|두려(?:웠|워|운|움)|afraid|fear",
    "HAPPY": r"행복|기뻤|기뻐|happy",
    "EXCITED": r"신났|신나|설렜|excited",
    "SAD": r"슬펐|슬퍼|슬픔|sad",
    "NERVOUS": r"긴장|불안|nervous",
    "ANGRY": r"화났|화나|화가|분노|angry",
}


def empty(route: Literal["CLARIFY", "NO_RECORD"] = "CLARIFY"):
    return SecureAskResponse(
        gate_route=route,
        answer="사건과 사람, 확인할 내용을 더 구체적으로 알려 주세요."
        if route == "CLARIFY"
        else "확인할 수 있는 기록이 없습니다.",
        citations=[],
        gate_action="llm_not_called",
        provider_mode="not_called",
    )


def plan(runtime, uid, room, payload):
    if payload.structured_query is not None:
        return None
    if payload.claim_query is not None:
        return (
            (payload.claim_query, None, None)
            if payload.claim_query.operation in OPERATIONS
            else None
        )
    q = payload.query.strip().rstrip("?？.")
    emotion = next(
        (k for k, v in EMOTIONS.items() if re.search(v, q, re.IGNORECASE)), None
    )
    if (
        not emotion
        and "기억" in q
        and any(w in q for w in ("다른", "차이", "비교", "충돌"))
    ):
        return None  # Existing bounded M5.1 comparison planner.
    relevant = emotion or "기억" in q or re.search(r"누구 기록으로|실제로 누가 참여", q)
    if not relevant:
        return None
    resolved = cast(
        AskQueryContext, context.query(runtime, uid, room, "resolve_ask_query", q)
    )
    if resolved["status"] != "RESOLVED":
        return (None, None, resolved)
    rewritten = resolved["query"]
    for name in sorted(set(resolved["names"]), key=len, reverse=True):
        rewritten = rewritten.replace(name, "MEMBER")
    members = sorted(
        {m for b in resolved["bindings"] for m in b["resolved_member_ids"]}
    )
    patterns = [
        (
            "PERSPECTIVE_COMPARE",
            r"(?P<topic>.+?)에서 MEMBER(?:이|가)? 한 행동은 누구 기록으로 알 수 있나",
        ),
        (
            "PARTICIPATION_MEMORY_COMPARE",
            r"(?P<topic>.+?)(?:에는|에서) 누가 참여했고 지금 누가 기억하나",
        ),
        (
            "PARTICIPATION_MEMORY_COMPARE",
            r"(?:(?P<topic>.+?)에서 )?(?:실제로 누가 참여했나|참여했지만 기억하지 못하는 사람은)",
        ),
        (
            "MEMORY_STATE",
            r"(?:(?P<topic>.+?)(?:에서|을|를) )?누가 (?:그 일을 )?(?:기억하나|기억하지 못하나|직접 기억하고 누가 나중에 들었나)",
        ),
    ]
    if emotion:
        patterns = [
            (
                "MENTAL_STATE_QUERY",
                r"MEMBER(?:이는|은|는|이|가)? (?:(?P<topic>.+?)에서 )?(?:"
                + EMOTIONS[emotion]
                + r")(?:다고 단정할 수 있나|했나|했어|었어|었나|나|어|는가|다고 할 수 있나)",
            )
        ]
    for operation, pattern in patterns:
        match = re.fullmatch(pattern, rewritten)
        if match:
            return (
                ClaimQuery.model_validate(
                    {
                        "operation": operation,
                        "subject_member_ids": members,
                        "emotion": emotion,
                    }
                ),
                match.groupdict().get("topic"),
                resolved,
            )
    return (None, None, resolved)


def participation_snapshot(runtime, uid, room):
    """Return authorized source-pinned M3 rows, never infer attendance from claims."""
    from app.services import family_context as plain
    from app.services.structured_query import sources

    _, saved, state, container = context._read(runtime, uid, room)
    # Match existing M6 stale behavior; hidden components cannot trigger it.
    if any(
        c["kind"] in {"EVENT", "PARTICIPATION"}
        and all(s["record_id"] in state["allowed"] for s in c["sources"])
        and not context.eligible(c, state)
        for p in container.get("packages", [])
        for c in p["components"]
    ):
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    result = []
    with context.projection(state) as db:
        context._load(db, container, state, room)
        members = {
            m.user_id: m.id
            for m in db.scalars(
                select(FamilyMember).where(
                    FamilyMember.room_id == room, FamilyMember.is_active.is_(True)
                )
            )
        }
        people = {
            p.id: p
            for p in db.scalars(plain.visible_people(db, room))
            if p.user_id in members
        }
        parts = list(
            db.scalars(
                select(EventParticipation).where(
                    EventParticipation.id.in_(
                        plain.entity_ids(db, uid, room, "PARTICIPATION")
                    )
                )
            )
        )
        for event in db.scalars(
            select(Event).where(Event.id.in_(plain.entity_ids(db, uid, room, "EVENT")))
        ):
            result.append(
                {
                    "event_id": event.id,
                    "title": event.title,
                    "event_status": event.event_status,
                    "event_occurred_at": event.occurred_at,
                    "sources": [s.model_dump() for s in sources(db, [event.id])],
                    "participation": [
                        {
                            "member_id": members[
                                cast(str, people[p.person_id].user_id)
                            ],
                            "member_label": people[p.person_id].display_name,
                            "mode": p.mode,
                            "phase_ref": p.phase_ref,
                            "occurred_at": p.occurred_at,
                            "sources": [s.model_dump() for s in sources(db, [p.id])],
                        }
                        for p in parts
                        if p.event_id == event.id and p.person_id in people
                    ],
                }
            )
    with context.ops.tx() as db:
        context._revalidate(db, saved, state, uid, room)
    return sorted(result, key=lambda e: e["event_id"])


def establishes_emotion(claim, emotion):
    return (
        claim.proposition.predicate == "EMOTION"
        and claim.proposition.value == emotion
        and claim.explicit_mental_state
        and claim.certainty == "CERTAIN"
        and claim.proposition.polarity in {"AFFIRMED", "NEGATED"}
        and claim.epistemic_basis not in {"INTERPRETATION", "UNKNOWN"}
    )


def answer(runtime, uid, room, payload):
    from app.services import claims as journal
    from app.services.secure_retrieval import materialize_room, revalidate_evidence
    from app.services.structured_answer import _snapshot

    planned = plan(runtime, uid, room, payload)
    if planned is None:
        return None
    query, topic, resolved = planned
    if query is None or (query.operation == "MENTAL_STATE_QUERY" and not query.emotion):
        return empty()
    start = perf_counter()
    values, state = journal.admitted(runtime, uid, room, query)
    corpus = materialize_room(runtime, uid, room)
    # Validate before scope selection/counts/answerability, including stale candidates.
    for claim in values:
        journal._validate(claim, corpus, state)
    retrieval_ms = (perf_counter() - start) * 1000
    part_start = perf_counter()
    events = participation_snapshot(runtime, uid, room)
    if topic:
        # Activity suffixes are grammar, not inferred event aliases. Match the
        # remaining literal topic only against reviewed M3 titles, never prose.
        title_topic = re.sub(r"\s+(?:체험|행사|방문)$", "", topic).strip()
        matches = [
            e
            for e in events
            if title_topic and title_topic.casefold() in e["title"].casefold()
        ]
        if len(matches) != 1:
            return empty("CLARIFY" if matches else "NO_RECORD")
        event_id = matches[0]["event_id"]
    elif query.event_id:
        event_id = query.event_id
    else:
        scoped = [
            c
            for c in values
            if not query.subject_member_ids
            or c.subject_member_id in query.subject_member_ids
        ]
        ids = {c.event_id for c in scoped}
        if len(ids) != 1 or None in ids:
            return empty("CLARIFY" if ids else "NO_RECORD")
        event_id = next(iter(ids))
    selected_events = [e for e in events if e["event_id"] == event_id]
    scoped_values = [c for c in values if c.event_id == event_id]
    if query.operation == "MENTAL_STATE_QUERY":
        if not query.subject_member_ids:
            return empty()
        scoped_values = [
            c for c in scoped_values if c.subject_member_id in query.subject_member_ids
        ]
        selected = [
            c
            for c in scoped_values
            if establishes_emotion(c, query.emotion)
            or (
                c.proposition.predicate in {"ACTION", "MEMORY_STATE"}
                and c.epistemic_basis != "INTERPRETATION"
            )
        ]
    elif query.operation == "MEMORY_STATE":
        selected = [
            c
            for c in scoped_values
            if c.proposition.predicate == "MEMORY_STATE"
            and (
                not query.subject_member_ids
                or c.subject_member_id in query.subject_member_ids
            )
        ]
    else:
        selected = [c for c in scoped_values if c.proposition.predicate != "EMOTION"]
    part_ms = (perf_counter() - part_start) * 1000
    # Event sources cannot become proof of an internal state. Only show M3 rows
    # for the explicit participation comparison or source-perspective question.
    included_events = (
        selected_events
        if query.operation in {"PARTICIPATION_MEMORY_COMPARE", "PERSPECTIVE_COMPARE"}
        else []
    )
    support = [
        s
        for e in included_events
        for s in e["sources"] + [s for p in e["participation"] for s in p["sources"]]
    ]
    if not selected and not support:
        return empty("NO_RECORD")
    rel_start = perf_counter()
    relations = [
        {
            "left": a.claim_id,
            "right": b.claim_id,
            "relation": journal.relationship(a, b),
        }
        for a, b in combinations(selected, 2)
    ]
    rel_ms = (perf_counter() - rel_start) * 1000
    render_start = perf_counter()
    ids = {c.source.record_id for c in selected} | {s["record_id"] for s in support}
    rendered = grounded.render_secure_grounded_answer(
        runtime,
        _snapshot(
            payload,
            SimpleNamespace(
                entries=[
                    SimpleNamespace(sources=[SimpleNamespace(record_id=r)]) for r in ids
                ]
            ),
            corpus.chunks,
            corpus,
        ),
    )
    citations, perspectives, lines = [], [], []
    for claim in selected:
        cite = next(
            c for c in rendered.citations if c.chunk_id == claim.source.chunk_id
        )
        citations.append(
            cite.model_copy(
                update={
                    "original_url": cite.original_url
                    + "&"
                    + urlencode(
                        {
                            "claim_id": claim.claim_id,
                            "claim_version": claim.claim_version,
                        }
                    ),
                    "claim_id": claim.claim_id,
                    "claim_version": claim.claim_version,
                    "claim_quote_start": claim.source.quote_start,
                    "claim_quote_end": claim.source.quote_end,
                }
            )
        )
        perspectives.append(
            {
                "claim_id": claim.claim_id,
                "claim_version": claim.claim_version,
                "claimant_member_id": claim.claimant_member_id,
                "subject_member_id": claim.subject_member_id,
                "source_narrator_member_id": claim.source_narrator_member_id,
                "epistemic_basis": claim.epistemic_basis,
                "perspective_type": claim.perspective_type,
                "proposition": claim.proposition.model_dump(),
                "review_status": "APPROVED",
                "event_occurred_at": claim.event_occurred_at.model_dump()
                if claim.event_occurred_at
                else None,
                "recorded_at": claim.recorded_at.model_dump()
                if claim.recorded_at
                else None,
                "recollection_source_date": claim.recollection_source_date.model_dump()
                if claim.recollection_source_date
                else None,
                "citation_index": len(citations) - 1,
            }
        )
        lines.append(
            f"{claim.claimant_label} ({claim.epistemic_basis}; {claim.proposition.predicate}={claim.proposition.value}; {claim.proposition.polarity}): “{claim.quote}” [{len(citations)}]"
        )
    participation = []
    for event in included_events:
        lines.append(f"사건: {event['title']}; 발생 시점: {event['event_occurred_at']}")
        for part in event["participation"]:
            indices = []
            for source in part["sources"]:
                for cite in rendered.citations:
                    if cite.record_id == source["record_id"]:
                        citations.append(cite)
                        indices.append(len(citations) - 1)
            participation.append(
                dict(part, event_id=event_id, citation_indices=indices)
            )
            lines.append(
                f"{part['member_label']}: 참여={part['mode']} "
                + " ".join(f"[{i + 1}]" for i in indices)
            )
        for source in event["sources"]:
            for cite in rendered.citations:
                if cite.record_id == source["record_id"] and cite not in citations:
                    citations.append(cite)
    emotion_answerable = (
        any(establishes_emotion(c, query.emotion) for c in selected)
        if query.operation == "MENTAL_STATE_QUERY"
        else None
    )
    emotion_supported = (
        any(
            establishes_emotion(c, query.emotion)
            and c.proposition.polarity == "AFFIRMED"
            for c in selected
        )
        if query.operation == "MENTAL_STATE_QUERY"
        else None
    )
    if emotion_answerable is False:
        lines.append(
            "확인 가능한 승인 기록으로는 해당 감정을 확정할 수 없습니다. 행동이나 기억 부재로 감정을 추정하지 않습니다."
        )
    if query.operation == "PARTICIPATION_MEMORY_COMPARE":
        lines.append(
            "참여와 기억은 별개입니다. 명시된 기억 상태가 없는 사람의 기억 여부는 확인되지 않았습니다."
        )
    current, _ = journal.admitted(runtime, uid, room, query)
    if current != values or participation_snapshot(runtime, uid, room) != events:
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    if (
        resolved is not None
        and context.query(
            runtime,
            uid,
            room,
            "resolve_ask_query",
            payload.query.strip().rstrip("?？."),
        )
        != resolved
    ):
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    revalidate_evidence(runtime, corpus)
    conflict = any(r["relation"] == "CONTRADICTS" for r in relations)
    return rendered.model_copy(
        update={
            "gate_route": "CONFLICT" if conflict else "ANSWER",
            "answer": "\n".join(lines),
            "citations": citations,
            "gate_action": "claim_render",
            "provider_mode": "not_called",
            "claim_result": {
                "operation": query.operation,
                "viewer_member_id": state["actor"]["id"],
                "authorized_claims_count": len(selected),
                "relations": relations,
                "perspectives": perspectives,
                "participation": participation,
                "emotion_supported": emotion_supported,
                "emotion_answerable": emotion_answerable,
                "claim_retrieval_ms": retrieval_ms,
                "participation_lookup_ms": part_ms,
                "relationship_ms": rel_ms,
                "rendering_ms": (perf_counter() - render_start) * 1000,
            },
        }
    )
