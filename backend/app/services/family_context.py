"""Deterministic M3 context. All record-derived metadata is filtered in SQL."""
from datetime import date, datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import exists, or_, select, text
from sqlalchemy.orm import Session

from app.models.family import FamilyMember, FamilyPerson, FamilyRoom
from app.models.family_context import (
    AliasTarget,
    ContextEntity,
    ContextSource,
    Event,
    EventParticipation,
    FamilyRelation,
    RelationAlias,
)
from app.models.record import Record
from app.schemas.family_context import (
    ContextFilters,
    QueryContext,
    Resolution,
    TemporalValue,
)
from app.services.record_policy import allowed_record_ids


def unavailable() -> HTTPException:
    return HTTPException(404, detail={"code": "RESOURCE_NOT_AVAILABLE"})


def membership(db: Session, user_id: str, room_id: str) -> FamilyMember:
    member = db.scalar(select(FamilyMember).join(FamilyRoom, FamilyRoom.id == FamilyMember.room_id).where(
        FamilyMember.room_id == room_id, FamilyMember.user_id == user_id,
        FamilyMember.is_active.is_(True), FamilyRoom.is_active.is_(True)))
    tombstone = db.execute(text("SELECT 1 FROM security_tombstones WHERE (domain='room' AND owner_id=:room AND target_kind='room') OR (domain='user' AND owner_id=:user) LIMIT 1"), {"room": room_id, "user": user_id}).first()
    if member is None or tombstone is not None:
        raise unavailable()
    return member


def visible_people(db: Session, room_id: str):
    active = select(FamilyMember.user_id).where(FamilyMember.room_id == room_id, FamilyMember.is_active.is_(True))
    return select(FamilyPerson).where(FamilyPerson.room_id == room_id, or_(FamilyPerson.user_id.is_(None), FamilyPerson.user_id.in_(active)))


def entity_ids(db: Session, user_id: str, room_id: str, kind: str):
    """No title/alias/participation materialization until all source ACLs pass."""
    membership(db, user_id, room_id)
    allowed = allowed_record_ids(db, user_id, room_id, purpose="rag")
    invalid = exists(select(ContextSource.entity_id).outerjoin(Record, Record.id == ContextSource.record_id).where(
        ContextSource.entity_id == ContextEntity.id,
        or_(ContextSource.record_id.not_in(allowed), Record.id.is_(None), Record.room_id != room_id,
            Record.record_version != ContextSource.record_version, Record.source_revision != ContextSource.source_revision)))
    has_source = exists(select(ContextSource.entity_id).where(ContextSource.entity_id == ContextEntity.id))
    conditions = [ContextEntity.room_id == room_id, ContextEntity.kind == kind,
                  ContextEntity.review_status == "APPROVED", ~invalid]
    if kind in ("EVENT", "PARTICIPATION"):
        conditions.append(has_source)
    return select(ContextEntity.id).where(*conditions)


def _valid_at(start: dict[str, Any], end: dict[str, Any], as_of: date) -> bool:
    for value, lower in ((start, True), (end, False)):
        temporal = TemporalValue.model_validate(value)
        if temporal.precision == "UNKNOWN":
            continue
        bound = temporal.latest_known_day()
        if bound is None or (lower and as_of < bound) or (not lower and as_of > bound):
            return False
    return True


def resolve(db: Session, user_id: str, room_id: str, surface: str, as_of: date | None = None) -> Resolution:
    member = membership(db, user_id, room_id)
    day = as_of or datetime.now(ZoneInfo("Asia/Seoul")).date()
    people = {p.id: p for p in db.scalars(visible_people(db, room_id))}
    speaker = next((p.id for p in people.values() if p.user_id == member.user_id), None)
    candidates = {p.id for p in people.values() if p.display_name == surface}
    basis = "EXACT_NAME"
    if not candidates and speaker is not None:
        basis = "APPROVED_ALIAS"
        aliases = db.scalars(select(RelationAlias).where(
            RelationAlias.id.in_(entity_ids(db, user_id, room_id, "ALIAS")),
            RelationAlias.speaker_person_id == speaker, RelationAlias.alias == surface,
            or_(RelationAlias.viewer_person_id.is_(None), RelationAlias.viewer_person_id == speaker)))
        for alias in aliases:
            if _valid_at(alias.valid_from, alias.valid_to, day):
                candidates.update(db.scalars(select(AliasTarget.person_id).where(AliasTarget.alias_id == alias.id, AliasTarget.person_id.in_(people))))
    ids = sorted(candidates)
    status = "RESOLVED" if len(ids) == 1 else "AMBIGUOUS" if ids else "UNRESOLVED"
    member_ids = []
    if len(ids) == 1 and people[ids[0]].user_id:
        member_ids = list(db.scalars(select(FamilyMember.id).where(FamilyMember.room_id == room_id,
            FamilyMember.user_id == people[ids[0]].user_id, FamilyMember.is_active.is_(True))))
    return Resolution(expression=surface, status=status, resolved_person_id=ids[0] if len(ids) == 1 else None,
        candidate_person_ids=ids, resolved_member_ids=member_ids, speaker_person_id=speaker, basis=basis,
        clarification=None if len(ids) == 1 else "어느 분을 말씀하시는 건가요?", as_of=day)


def relations(db: Session, user_id: str, room_id: str, as_of: date | None = None) -> list[dict[str, Any]]:
    day = as_of or datetime.now(ZoneInfo("Asia/Seoul")).date()
    people = visible_people(db, room_id).with_only_columns(FamilyPerson.id)
    rows = db.scalars(select(FamilyRelation).where(FamilyRelation.id.in_(entity_ids(db, user_id, room_id, "RELATION")),
        FamilyRelation.from_person_id.in_(people), FamilyRelation.to_person_id.in_(people)))
    return [{"relation_id": x.id, "from_person_id": x.from_person_id, "to_person_id": x.to_person_id,
             "type": x.relation_type, "valid_from": x.valid_from, "valid_to": x.valid_to}
            for x in rows if _valid_at(x.valid_from, x.valid_to, day)]


def events(db: Session, user_id: str, room_id: str, *, mode: str | None = None,
           person_id: str | None = None, status: str = "COMPLETED", as_of: date | None = None) -> list[dict[str, Any]]:
    day = as_of or datetime.now(ZoneInfo("Asia/Seoul")).date()
    eligible_events = entity_ids(db, user_id, room_id, "EVENT")
    eligible_participation = entity_ids(db, user_id, room_id, "PARTICIPATION")
    people = visible_people(db, room_id).with_only_columns(FamilyPerson.id)
    if person_id is not None and not db.scalar(visible_people(db, room_id).where(FamilyPerson.id == person_id)):
        raise unavailable()
    query = select(Event).where(Event.id.in_(eligible_events), Event.event_status == status)
    if mode is not None or person_id is not None:
        participation = select(EventParticipation.event_id).where(EventParticipation.id.in_(eligible_participation), EventParticipation.person_id.in_(people))
        if mode is not None:
            participation = participation.where(EventParticipation.mode == mode)
        if person_id is not None:
            participation = participation.where(EventParticipation.person_id == person_id)
        query = query.where(Event.id.in_(participation))
    result = []
    for event in db.scalars(query.order_by(Event.id)):
        time = TemporalValue.model_validate(event.occurred_at)
        latest = time.latest_known_day()
        # Imprecise dates remain eligible only if their known year is strictly past.
        past_year = time.value is not None and time.value[:4].isdigit() and int(time.value[:4]) < day.year
        if status == "COMPLETED" and not ((latest is not None and latest <= day) or (latest is None and past_year)):
            continue
        rows = db.scalars(select(EventParticipation).where(EventParticipation.event_id == event.id,
            EventParticipation.id.in_(eligible_participation), EventParticipation.person_id.in_(people)))
        result.append({"event_id": event.id, "title": event.title, "event_type": event.category,
            "occurred_at": event.occurred_at, "event_status": event.event_status, "location": event.location,
            "participation": [{"person_id": x.person_id, "mode": x.mode, "phase_ref": x.phase_ref,
                               "occurred_at": x.occurred_at} for x in rows]})
    return result


def query_context(db: Session, user_id: str, room_id: str, filters: ContextFilters) -> QueryContext:
    viewer = membership(db, user_id, room_id)
    selected = set(filters.resolved_member_ids + filters.subject_member_ids + filters.participant_member_ids + filters.excluded_member_ids)
    allowed = set(db.scalars(select(FamilyMember.id).where(FamilyMember.room_id == room_id, FamilyMember.is_active.is_(True))))
    if not selected <= allowed:
        raise unavailable()
    if set(filters.participant_member_ids) & set(filters.excluded_member_ids):
        raise HTTPException(422, detail={"code": "CONTRADICTORY_CONTEXT"})
    values = filters.model_dump()
    values["as_of"] = filters.as_of or datetime.now(ZoneInfo("Asia/Seoul")).date()
    return QueryContext(viewer_member_id=viewer.id, family_room_id=room_id, **values)


def accessible_event_ids(db: Session, user_id: str, room_id: str) -> list[str]:
    """Authorized opaque event links for additive reviewed-media metadata."""
    return list(db.scalars(entity_ids(db, user_id, room_id, "EVENT")))


class AskQueryContext(TypedDict):
    query: str
    status: str
    bindings: list[dict[str, Any]]
    names: list[str]


class RecordDiscoveryContext(AskQueryContext):
    record_ids: list[str] | None


def resolve_record_discovery(db: Session, user_id: str, room_id: str,
                             query: str) -> RecordDiscoveryContext:
    from app.ai.gates.record_discovery import audio_discovery, referenced_people
    from app.models.record import RecordPerson
    from app.models.upload import Upload
    import re
    context = resolve_ask_query(db, user_id, room_id, query)
    record_ids = None
    if context["bindings"]:
        allowed = set(allowed_record_ids(db, user_id, room_id, purpose="rag"))
        audio = audio_discovery(query)
        if audio:
            allowed.intersection_update(db.scalars(select(Upload.id).where(
                Upload.room_id == room_id, Upload.type == "voice")))
        subject_events = select(EventParticipation.event_id).where(
            EventParticipation.id.in_(entity_ids(db, user_id, room_id, "PARTICIPATION")),
            EventParticipation.phase_ref == "SUBJECT",
            EventParticipation.event_id.in_(entity_ids(db, user_id, room_id, "EVENT")),
        )
        explicit_sources = select(ContextSource.record_id).where(ContextSource.entity_id.in_(subject_events))
        for binding in context["bindings"]:
            if audio and not re.search(re.escape(binding["expression"]) + r"(?:에\s*대한|에\s*관한)", query):
                records = set(db.scalars(select(RecordPerson.record_id).where(
                    RecordPerson.member_id.in_(binding["resolved_member_ids"]),
                    RecordPerson.role == "PERSPECTIVE", RecordPerson.approved.is_(True),
                    RecordPerson.record_id.in_(allowed))))
                allowed.intersection_update(records)
                continue
            subjects = subject_events.where(EventParticipation.person_id == binding["resolved_person_id"])
            records = set(db.scalars(select(ContextSource.record_id).where(ContextSource.entity_id.in_(subjects))))
            records.update(db.scalars(select(RecordPerson.record_id).where(
                RecordPerson.member_id.in_(binding["resolved_member_ids"]),
                RecordPerson.role == "SUBJECT", RecordPerson.approved.is_(True),
                RecordPerson.record_id.in_(allowed), RecordPerson.record_id.not_in(explicit_sources))))
            allowed.intersection_update(records)
        record_ids = sorted(allowed)
    elif referenced_people(query):
        context["status"] = "CLARIFY"
    return {**context, "record_ids": record_ids}


def resolve_ask_query(db: Session, user_id: str, room_id: str, query: str) -> AskQueryContext:
    """Bind viewer-approved names/aliases before retrieval; no kinship dictionary.

    Return identities as a revalidation fence, never as citation evidence.
    Longest surfaces win and Korean particles are preserved.
    """
    import re
    membership(db, user_id, room_id)
    people = {p.id: p for p in db.scalars(visible_people(db, room_id))}
    aliases = set(db.scalars(select(RelationAlias.alias).where(
        RelationAlias.id.in_(entity_ids(db, user_id, room_id, "ALIAS")))))
    surfaces = aliases | {p.display_name for p in people.values()}
    bindings: list[dict[str, Any]] = []
    names: list[str] = []
    status = "RESOLVED"
    # Existing lexical inventory is used only to fail closed, never to map a
    # kinship term to a person. Actual mappings come exclusively from M3.
    from app.ai.gates.entity_index import KINSHIP
    from app.ai.gates.stored_memory import request as stored_request
    unresolved_relatives = {term for term in KINSHIP if term in query}
    if not surfaces:
        if stored_request(query) and unresolved_relatives:
            status = "CLARIFY"
        return {"query": query, "status": status, "bindings": bindings, "names": names}
    pattern = re.compile(r"(?<![가-힣])(" + "|".join(re.escape(s) for s in sorted(surfaces, key=lambda s: (-len(s), s)))
                         + r")(?=(?:은|는|이|가|을|를|의|에게|께서|에)?(?:\s|[?？,.]|$))")
    def replace(match: re.Match[str]) -> str:
        nonlocal status
        resolution = resolve(db, user_id, room_id, match.group())
        bindings.append(resolution.model_dump(mode="json"))
        if resolution.status != "RESOLVED":
            status = "CLARIFY"
            return match.group()
        if resolution.resolved_person_id is None:
            status = "CLARIFY"
            return match.group()
        name = people[resolution.resolved_person_id].display_name
        names.append(name)
        return name
    rewritten = pattern.sub(replace, query)
    if stored_request(query) and any(not any(term in b["expression"] for b in bindings)
                                     for term in unresolved_relatives):
        status = "CLARIFY"
    return {"query": rewritten, "status": status, "bindings": bindings, "names": names}


def structured_ask_query(db, user_id, room_id, query, filters=None, as_of=None):
    from app.services.structured_query import ask_query
    return ask_query(db, user_id, room_id, query, filters, as_of)
