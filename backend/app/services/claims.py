"""Persistent encrypted claim journal with source-first admission and typed comparison.

No source text, member labels or propositions are persisted in the catalogue.
The room envelope owns component wrapping keys and inventory/lifecycle cleanup.
"""

import re
from dataclasses import replace
from datetime import datetime, timezone
from itertools import combinations
from time import perf_counter
from typing import cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.ai import grounded_answering as grounded
from app.api.routes import secure_operations as ops
from app.api.routes.secure_media import abandon_staged
from app.schemas.claims import ClaimPayload, ClaimQuery, ClaimSource
from app.services import secure_catalogue as cat
from app.services import secure_family_context as context
from app.services.family_context import AskQueryContext


def _journal(room):
    value = room.get("m51_claims", {"format_version": 1, "versions": [], "events": []})
    if value["format_version"] != 1:
        raise cat.SecureStateChanged()
    return value


def _publish(runtime, uid, room_id, room, before, state, journal):
    updated = dict(room, m51_claims=journal)
    with ops.tx() as db:
        context._revalidate(db, before, state, uid, room_id)
    staged = ops.stage_update(runtime, uid, ("room", UUID(room_id)), updated)
    try:
        if replace(staged[0], operation_id=None) != replace(before, operation_id=None):
            raise cat.SecureStateChanged()
        with ops.tx() as db:
            cat.lock_rooms(db, (room_id,))
            context._revalidate(db, staged[0], state, uid, room_id)
            ops.publish_update(db, staged)
    except BaseException:
        abandon_staged([staged])
        raise


def _event(claim, status, reviewer):
    return {
        "claim_id": claim.claim_id,
        "claim_version": claim.claim_version,
        "status": status,
        "reviewer_member_id": reviewer,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def _status(journal, component):
    return next(
        e["status"]
        for e in reversed(journal["events"])
        if (e["claim_id"], e["claim_version"])
        == (component["claim_id"], component["claim_version"])
    )


def _latest(journal):
    latest = {}
    for component in journal["versions"]:
        if component["claim_version"] > latest.get(component["claim_id"], {}).get(
            "claim_version", 0
        ):
            latest[component["claim_id"]] = component
    return list(latest.values())


def _published(journal):
    # A candidate must not change trusted routing, even when it proposes a new
    # version of an existing approved claim. Approval publishes a version;
    # revocation removes that version without resurrecting older versions.
    versions = {(c["claim_id"], c["claim_version"]): c for c in journal["versions"]}
    current = {}
    for event in journal["events"]:
        if event["status"] == "APPROVED":
            key = event["claim_id"]
            if event["claim_version"] >= current.get(key, 0):
                current[key] = event["claim_version"]
    return [versions[(key, version)] for key, version in current.items()]


def _source_from(corpus, state, chunk, start, end):
    row = next(r for r in state["rows"]["records"] if r["id"] == chunk.record_id)
    source = next(s for s in corpus.records if s[1]["id"] == chunk.record_id)
    if source[3] is None:
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    identity, _, _, receipt = source[3]
    return ClaimSource(
        record_id=chunk.record_id,
        record_version=row["record_version"],
        source_revision=row["source_revision"],
        chunk_id=chunk.id,
        file_id=chunk.file_id,
        processing_job_id=chunk.processing_job_id,
        source_attempt=chunk.source_attempt,
        source_result_sha256=chunk.source_result_sha256,
        text_sha256=chunk.text_sha256,
        source_object_id=str(identity.object_id),
        source_object_version=identity.object_version,
        source_ciphertext_sha256=receipt.ciphertext_sha256.hex(),
        source_locator_json=chunk.source_locator_json,
        quote_start=start,
        quote_end=end,
    )


def bind_source(runtime, uid, room_id, record_id, quote):
    """Internal reviewed-fixture helper; preserves exact indexed source identity."""
    from app.services.secure_retrieval import materialize_room

    _, _, state, _ = context._read(runtime, uid, room_id)
    if record_id not in state["allowed"]:
        raise context.plain.unavailable()
    corpus = materialize_room(runtime, uid, room_id)
    matches = [
        (c, c.text.find(quote))
        for c in corpus.chunks
        if c.record_id == record_id and c.text.count(quote) == 1
    ]
    if len(matches) != 1:
        raise ValueError("quote must identify one exact source chunk")
    chunk, start = matches[0]
    return _source_from(corpus, state, chunk, start, start + len(quote))


def _validate(claim, corpus, state):
    chunk = next((c for c in corpus.chunks if c.id == claim.source.chunk_id), None)
    if chunk is None or chunk.room_id != claim.room_id:
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    expected = _source_from(
        corpus, state, chunk, claim.source.quote_start, claim.source.quote_end
    )
    if (
        expected != claim.source
        or chunk.text[claim.source.quote_start : claim.source.quote_end] != claim.quote
    ):
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    return chunk


def submit(runtime, uid, room_id, claim, status="PENDING_REVIEW"):
    """Publish an immutable candidate; trust is a separate explicit review action."""
    from app.services.secure_retrieval import materialize_room

    if status not in {"PREDICTED", "PENDING_REVIEW"}:
        raise ValueError("submission cannot approve a claim")
    room, before, state, _ = context._read(runtime, uid, room_id)
    if claim.room_id != room_id or claim.source.record_id not in state["allowed"]:
        raise context.plain.unavailable()
    if state["actor"]["role"].lower() not in {"owner", "admin"}:
        raise context.plain.unavailable()
    members = {m["id"] for m in state["rows"]["family_members"] if m["is_active"]}
    if any(
        m is not None and m not in members
        for m in (
            claim.claimant_member_id,
            claim.subject_member_id,
            claim.source_narrator_member_id,
        )
    ):
        raise ValueError("claim identities must belong to this family")
    _validate(claim, materialize_room(runtime, uid, room_id), state)
    journal = _journal(room)
    prior = next((c for c in _latest(journal) if c["claim_id"] == claim.claim_id), None)
    if claim.claim_version != (prior["claim_version"] + 1 if prior else 1):
        raise ValueError("immutable claim version already exists or lineage has a gap")
    if prior and prior["sources"][0]["record_id"] != claim.source.record_id:
        raise ValueError("a different source needs its own claim identity")
    component = context.seal_component(
        room_id, str(uuid4()), claim.model_dump(mode="json")
    )
    component.update(
        claim_id=claim.claim_id,
        claim_version=claim.claim_version,
        sources=[
            {
                k: getattr(claim.source, k)
                for k in ("record_id", "record_version", "source_revision")
            }
        ],
    )
    updated = dict(
        journal,
        versions=[*journal["versions"], component],
        events=[*journal["events"], _event(claim, status, state["actor"]["id"])],
    )
    _publish(runtime, uid, room_id, room, before, state, updated)
    return {
        "claim_id": claim.claim_id,
        "claim_version": claim.claim_version,
        "review_status": status,
    }


def review(runtime, uid, room_id, claim_id, version, status):
    from app.services.secure_retrieval import materialize_room

    if status not in {"PENDING_REVIEW", "APPROVED", "REJECTED"}:
        raise ValueError("invalid review state")
    room, before, state, _ = context._read(runtime, uid, room_id)
    if state["actor"]["role"].lower() not in {"owner", "admin"}:
        raise context.plain.unavailable()
    journal = _journal(room)
    component = next(
        (
            c
            for c in journal["versions"]
            if c["claim_id"] == claim_id
            and c["claim_version"] == version
            and c["sources"][0]["record_id"] in state["allowed"]
        ),
        None,
    )
    if component is None:
        raise context.plain.unavailable()
    claim = ClaimPayload.model_validate(context.open_component(room_id, component))
    if status == "APPROVED" and component not in _latest(journal):
        raise ValueError("superseded version cannot be republished")
    if status == "APPROVED":
        _validate(claim, materialize_room(runtime, uid, room_id), state)
    updated = dict(
        journal,
        events=[*journal["events"], _event(claim, status, state["actor"]["id"])],
    )
    _publish(runtime, uid, room_id, room, before, state, updated)
    return {"claim_id": claim_id, "claim_version": version, "review_status": status}


def admitted(runtime, uid, room_id, query):
    """ACL and review gates precede component opening, filtering and counts."""
    room, saved, state, _ = context._read(runtime, uid, room_id)
    journal = _journal(room)
    claims = []
    for c in _published(journal):
        if (
            c["sources"][0]["record_id"] not in state["allowed"]
            or _status(journal, c) != "APPROVED"
        ):
            continue
        claim = ClaimPayload.model_validate(context.open_component(room_id, c))
        if claim.room_id != room_id:
            raise cat.SecureStateChanged()
        if query.event_id is not None and claim.event_id != query.event_id:
            continue
        if (
            query.claimant_member_ids
            and claim.claimant_member_id not in query.claimant_member_ids
        ):
            continue
        claims.append(claim)
    with ops.tx() as db:
        context._revalidate(db, saved, state, uid, room_id)
    return sorted(claims, key=lambda c: (c.claim_id, c.claim_version)), state


def relationship(a, b):
    """No probabilistic truth selection. Unknown scopes never contradict."""
    x, y = a.proposition, b.proposition
    if a.event_id is None or b.event_id is None:
        return "UNRESOLVED"
    if a.event_id != b.event_id:
        return "INDEPENDENT_PERSPECTIVE"
    if x.temporal_scope and y.temporal_scope and x.temporal_scope != y.temporal_scope:
        return "INDEPENDENT_PERSPECTIVE"
    if a.epistemic_basis == "INTERPRETATION" or b.epistemic_basis == "INTERPRETATION":
        return "INDEPENDENT_PERSPECTIVE"
    if x.subject_id != y.subject_id:
        return "INDEPENDENT_PERSPECTIVE"
    if (x.predicate, x.dimension) != (y.predicate, y.dimension):
        return "SUPPLEMENTS"
    if x.predicate == y.predicate == "MEMORY_STATE" and (
        {x.value, y.value} & {"UNKNOWN", "HEARD_FROM_OTHER"}
    ):
        return "SUPPLEMENTS"
    if (
        not x.temporal_scope
        or not y.temporal_scope
        or "UNCERTAIN" in (x.polarity, y.polarity)
    ):
        return "UNRESOLVED"
    if (
        getattr(a, "certainty", "CERTAIN") != "CERTAIN"
        or getattr(b, "certainty", "CERTAIN") != "CERTAIN"
    ):
        return (
            "COMPATIBLE"
            if x.value == y.value and x.polarity == y.polarity
            else "UNRESOLVED"
        )
    if x.value == y.value:
        return "COMPATIBLE" if x.polarity == y.polarity else "CONTRADICTS"
    if x.exclusive and y.exclusive and x.polarity == y.polarity == "AFFIRMED":
        return "CONTRADICTS"
    return "UNRESOLVED"


def plan_query(payload):
    if payload.structured_query is not None:
        return None
    if payload.claim_query is not None:
        return payload.claim_query
    q = payload.query.casefold()
    if ("기억" in q and any(w in q for w in ("다른", "차이", "비교"))) or any(
        w in q
        for w in ("perspective", "contradict", "conflicting", "관점 비교", "기억 충돌")
    ):
        return ClaimQuery()
    return None


def answer(runtime, uid, room_id, payload):
    from types import SimpleNamespace

    from app.schemas.ai import SecureAskResponse
    from app.services import childhood_memory
    from app.services.secure_retrieval import materialize_room
    from app.services.structured_answer import _snapshot

    extended = childhood_memory.answer(runtime, uid, room_id, payload)
    if extended is not None:
        return extended
    query = plan_query(payload)
    if query is None:
        return None
    if payload.claim_query is None:
        resolved = cast(
            AskQueryContext,
            context.query(runtime, uid, room_id, "resolve_ask_query", payload.query),
        )
        member_ids = sorted(
            {m for b in resolved["bindings"] for m in b["resolved_member_ids"]}
        )
        remainder = resolved["query"]
        for name in resolved["names"]:
            remainder = remainder.replace(name, "")
        # A deliberately bounded natural-language grammar; unfamiliar topic or
        # person terms cannot silently broaden into all family claims.
        remainder = re.sub(
            r"직접|기억|관점|어떻게|다른가|다른지|차이|비교|알려|주세요|해줘|은|는|과|와|의|을|를|[\s?？,.]",
            "",
            remainder,
        )
        if resolved["status"] != "RESOLVED" or not member_ids or remainder:
            return SecureAskResponse(
                gate_route="CLARIFY",
                answer="비교할 사건과 사람을 더 구체적으로 알려 주세요.",
                citations=[],
                gate_action="llm_not_called",
                provider_mode="not_called",
            )
        query = query.model_copy(update={"claimant_member_ids": member_ids})
    started = perf_counter()
    claims, state = admitted(runtime, uid, room_id, query)
    corpus = materialize_room(runtime, uid, room_id)
    chunks = {}
    for c in claims:
        chunk = _validate(c, corpus, state)
        chunks[chunk.id] = chunk
    if payload.claim_query is None and "직접 기억" in payload.query:
        direct_events = {
            c.event_id
            for c in claims
            if c.perspective_type == "DIRECT_MEMORY"
            and c.epistemic_basis == "DIRECT_EXPERIENCE"
            and c.proposition.predicate == "MEMORY_STATE"
            and c.proposition.value in {"REMEMBERS", "FAINT"}
        }
        if claims and not direct_events:
            return SecureAskResponse(
                gate_route="CLARIFY",
                answer="직접 기억을 비교할 사건을 더 구체적으로 알려 주세요.",
                citations=[],
                gate_action="llm_not_called",
                provider_mode="not_called",
            )
        if direct_events:
            claims = [c for c in claims if c.event_id in direct_events]
    # Without an explicit event scope, several events require clarification.
    # No selection by hidden evidence, recency, or a remembered benchmark answer.
    if not claims:
        return SecureAskResponse(
            gate_route="NO_RECORD",
            answer="확인할 수 있는 기록이 없습니다.",
            citations=[],
            gate_action="llm_not_called",
            provider_mode="not_called",
        )
    if (
        query.operation != "CLAIM_TIMELINE"
        and query.event_id is None
        and (len({c.event_id for c in claims}) != 1 or claims[0].event_id is None)
    ):
        return SecureAskResponse(
            gate_route="CLARIFY",
            answer="어느 사건의 관점을 비교할까요?",
            citations=[],
            gate_action="llm_not_called",
            provider_mode="not_called",
        )
    if query.operation == "CLAIM_TIMELINE":
        from datetime import date

        try:
            for c in claims:
                if not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", c.proposition.temporal_scope or ""
                ):
                    raise ValueError()
                date.fromisoformat(c.proposition.temporal_scope)
        except ValueError:
            return SecureAskResponse(
                gate_route="CLARIFY",
                answer="주장 연표에는 확인된 날짜가 필요합니다.",
                citations=[],
                gate_action="llm_not_called",
                provider_mode="not_called",
            )
        claims.sort(key=lambda c: (c.proposition.temporal_scope, c.claim_id))
    retrieval_ms = (perf_counter() - started) * 1000
    rel_start = perf_counter()
    relations = [
        {"left": a.claim_id, "right": b.claim_id, "relation": relationship(a, b)}
        for a, b in combinations(claims, 2)
    ]
    relation_ms = (perf_counter() - rel_start) * 1000
    render_start = perf_counter()
    entries = [
        SimpleNamespace(sources=[SimpleNamespace(record_id=c.source.record_id)])
        for c in claims
    ]
    rendered = grounded.render_secure_grounded_answer(
        runtime,
        _snapshot(
            payload, SimpleNamespace(entries=entries), list(chunks.values()), corpus
        ),
    )
    citations, perspectives, lines = [], [], []
    for c in claims:
        citation = next(
            v for v in rendered.citations if v.chunk_id == c.source.chunk_id
        )
        citation = citation.model_copy(
            update={
                "original_url": citation.original_url
                + "&"
                + urlencode({"claim_id": c.claim_id, "claim_version": c.claim_version}),
                "claim_id": c.claim_id,
                "claim_version": c.claim_version,
                "claim_quote_start": c.source.quote_start,
                "claim_quote_end": c.source.quote_end,
            }
        )
        citations.append(citation)
        label = {
            "DIRECT_EXPERIENCE": "직접 경험",
            "RETROSPECTIVE_MEMORY": "회상",
            "HEARD_FROM_OTHER": "전해 들은 내용",
            "DOCUMENTED_SOURCE": "문서 기록",
            "INTERPRETATION": "해석",
            "UNKNOWN": "근거 유형 미확인",
        }[c.epistemic_basis]
        if c.proposition.predicate == "MEMORY_STATE":
            memory_state = {
                "REMEMBERS": "기억함",
                "FAINT": "흐릿하게 기억함",
                "DOES_NOT_REMEMBER": "기억하지 못함",
                "HEARD_FROM_OTHER": "전해 들어 알고 있음",
                "UNKNOWN": "기억 상태 미확인",
            }[c.proposition.value]
            if (
                c.epistemic_basis == "DIRECT_EXPERIENCE"
                and c.claimant_member_id == c.subject_member_id
                and c.source_narrator_member_id == c.subject_member_id
            ):
                label = f"본인 기억 상태: {memory_state}"
            else:
                label = f"{label}; 기억 상태: {memory_state}"
        when = (
            f"{c.proposition.temporal_scope} — "
            if query.operation == "CLAIM_TIMELINE"
            else ""
        )
        lines.append(
            f"{when}{c.claimant_label} ({label}): “{c.quote}” [{len(citations)}]"
        )
        perspectives.append(
            {
                "claim_id": c.claim_id,
                "claim_version": c.claim_version,
                "claimant_member_id": c.claimant_member_id,
                "subject_member_id": c.subject_member_id,
                "source_narrator_member_id": c.source_narrator_member_id,
                "epistemic_basis": c.epistemic_basis,
                "perspective_type": c.perspective_type,
                "proposition": c.proposition.model_dump(mode="json"),
                "temporal_scope": c.proposition.temporal_scope,
                "citation_index": len(citations) - 1,
            }
        )
    conflict = any(r["relation"] == "CONTRADICTS" for r in relations)
    if conflict:
        lines.append(
            "같은 범위의 주장에 불일치가 있습니다. 기록만으로 어느 쪽이 맞는지 결정하지 않습니다."
        )
    elif any(r["relation"] == "UNRESOLVED" for r in relations):
        lines.append("기록만으로 하나의 확정적인 설명을 정할 수 없습니다.")
    if (
        payload.claim_query is None
        and context.query(runtime, uid, room_id, "resolve_ask_query", payload.query)
        != resolved
    ):
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    current, _ = admitted(runtime, uid, room_id, query)
    if payload.claim_query is None and "직접 기억" in payload.query and direct_events:
        current_direct_events = {
            c.event_id
            for c in current
            if c.perspective_type == "DIRECT_MEMORY"
            and c.epistemic_basis == "DIRECT_EXPERIENCE"
            and c.proposition.predicate == "MEMORY_STATE"
            and c.proposition.value in {"REMEMBERS", "FAINT"}
        }
        if current_direct_events != direct_events:
            raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
        current = [c for c in current if c.event_id in direct_events]
    if sorted(current, key=lambda c: (c.claim_id, c.claim_version)) != sorted(
        claims, key=lambda c: (c.claim_id, c.claim_version)
    ):
        raise grounded.GroundedEvidenceChanged("STALE_SOURCE")
    # Full source/ACL validation also covers changes during deterministic rendering.
    from app.services.secure_retrieval import revalidate_evidence

    revalidate_evidence(runtime, corpus)
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
                "authorized_claims_count": len(claims),
                "relations": relations,
                "perspectives": perspectives,
                "claim_retrieval_ms": retrieval_ms,
                "relationship_ms": relation_ms,
                "rendering_ms": (perf_counter() - render_start) * 1000,
            },
        }
    )


def validate_citation(runtime, uid, room_id, record_id, claim_id, version, params):
    claims, state = admitted(runtime, uid, room_id, ClaimQuery())
    claim = next(
        (
            c
            for c in claims
            if c.claim_id == claim_id
            and c.claim_version == version
            and c.source.record_id == record_id
        ),
        None,
    )
    if claim is None:
        raise HTTPException(409, "STALE_SOURCE")
    expected = {
        key: str(getattr(claim.source, key))
        for key in (
            "file_id",
            "source_object_id",
            "source_object_version",
            "processing_job_id",
            "source_attempt",
            "source_result_sha256",
        )
    }
    expected["expected_ciphertext_sha256"] = claim.source.source_ciphertext_sha256
    if any(params.getlist(k) != [v] for k, v in expected.items()):
        raise HTTPException(409, "STALE_SOURCE")
    from app.services.secure_retrieval import materialize_room

    try:
        _validate(claim, materialize_room(runtime, uid, room_id), state)
    except grounded.GroundedEvidenceChanged:
        raise HTTPException(409, "STALE_SOURCE") from None
