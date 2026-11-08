"""Deterministic structured queries over the authorized M3 scope.

Calendar month/year bounds constrain intervals; they do not turn an uncertain
value into an exact date. Seasons/approximations without explicit bounds stay
unordered. Overlapping intervals fail closed for extrema and strict timelines.
"""

import calendar
import itertools
import re
from datetime import date, datetime, time, timedelta, timezone
from time import perf_counter
from zoneinfo import ZoneInfo

from sqlalchemy import exists, or_, select

from app.models.family import FamilyMember
from app.models.family_context import (
    ContextEntity,
    ContextSource,
    Event,
    EventParticipation,
)
from app.models.record import Record
from app.schemas.family_context import ContextFilters, TemporalValue
from app.schemas.structured_query import (
    EventType,
    StructuredEntry,
    StructuredFilters,
    StructuredPlan,
    StructuredResult,
    StructuredSource,
)
from app.services import family_context as context
from app.services.record_policy import allowed_record_ids


def day_bound(day, end=False, zone="Asia/Seoul"):
    return datetime.combine(
        day, time.max if end else time.min, ZoneInfo(zone)
    ).astimezone(timezone.utc)


def bounds(value: TemporalValue):
    def point(raw, end=False):
        if value.granularity == "INSTANT":
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        if value.granularity == "DAY":
            day = date.fromisoformat(raw)
        elif value.granularity == "MONTH":
            y, m = map(int, raw.split("-"))
            day = date(y, m, calendar.monthrange(y, m)[1] if end else 1)
        elif value.granularity == "YEAR":
            day = date(int(raw), 12 if end else 1, 31 if end else 1)
        else:
            return None
        return day_bound(day, end, value.timezone)

    if value.precision == "UNKNOWN":
        return None
    if value.precision == "APPROXIMATE" and not (value.start and value.end):
        return None
    try:
        first, last = (
            point(value.start or value.value),
            point(value.end or value.value, True),
        )
    except (OverflowError, ValueError):
        return None  # Source precision survives; no invented/clamped instant.
    return (first, last) if first is not None and last is not None else None


def plan_for(db, uid, room, filters: StructuredFilters):
    verified = context.query_context(
        db,
        uid,
        room,
        ContextFilters(
            subject_member_ids=filters.subject_member_ids
            + (
                [filters.during_subject_member_id]
                if filters.during_subject_member_id
                else []
            ),
            participant_member_ids=filters.participant_member_ids,
            excluded_member_ids=filters.excluded_member_ids,
            as_of=filters.as_of,
        ),
    )
    return StructuredPlan(
        **(
            filters.model_dump()
            | {
                "viewer_member_id": verified.viewer_member_id,
                "family_room_id": room,
                "as_of": verified.as_of,
            }
        )
    )


def reject_stale_scope(db, uid, room):
    """Authorized stale sources cannot silently promote an older latest result."""
    from app.ai.grounded_answering import GroundedEvidenceChanged

    allowed = allowed_record_ids(db, uid, room, purpose="rag")
    denied = exists(
        select(ContextSource.entity_id).where(
            ContextSource.entity_id == ContextEntity.id,
            ContextSource.record_id.not_in(allowed),
        )
    )
    stale = exists(
        select(ContextSource.entity_id)
        .join(Record, Record.id == ContextSource.record_id)
        .where(
            ContextSource.entity_id == ContextEntity.id,
            or_(
                Record.record_version != ContextSource.record_version,
                Record.source_revision != ContextSource.source_revision,
            ),
        )
    )
    if db.scalar(
        select(ContextEntity.id)
        .where(
            ContextEntity.room_id == room,
            ContextEntity.kind.in_(["EVENT", "PARTICIPATION"]),
            ContextEntity.review_status == "APPROVED",
            ~denied,
            stale,
        )
        .limit(1)
    ):
        raise GroundedEvidenceChanged("STALE_SOURCE")


def execute(db, uid, room, plan: StructuredPlan) -> StructuredResult:
    started = perf_counter()
    if (
        plan_for(
            db,
            uid,
            room,
            StructuredFilters(
                **plan.model_dump(exclude={"viewer_member_id", "family_room_id"})
            ),
        )
        != plan
    ):
        raise context.unavailable()

    def result(route, **kwargs):
        return StructuredResult(
            route=route,
            plan=plan,
            execution_ms=(perf_counter() - started) * 1000,
            **kwargs,
        )

    reject_stale_scope(db, uid, room)
    # All entity materialization follows ACL, review, room and version admission.
    event_query = select(Event).where(
        Event.id.in_(context.entity_ids(db, uid, room, "EVENT"))
    )
    interval_sources = []
    if plan.related_event_id:
        parent = db.scalar(event_query.where(Event.id == plan.related_event_id))
        if parent is None:
            return result("NO_RECORD", reason="NO_AUTHORIZED_PARENT")
        interval_sources.extend(sources(db, [parent.id]))
        if plan.compare_gift_use and parent.phase_ref != "GIFT_RECEIVED":
            return result("CLARIFY", reason="GIFT_RECEIPT_ROOT_REQUIRED")
    categories = plan.event_types or ([plan.event_type] if plan.event_type else [])
    scoped = list(
        db.scalars(
            event_query.where(
                Event.event_status == plan.event_status,
                *([Event.category.in_(categories)] if categories else []),
            ).order_by(Event.id)
        )
    )
    people = list(db.scalars(context.visible_people(db, room)))
    members = {
        m.user_id: m.id
        for m in db.scalars(
            select(FamilyMember).where(
                FamilyMember.room_id == room, FamilyMember.is_active.is_(True)
            )
        )
    }
    person_members = {p.id: members[p.user_id] for p in people if p.user_id in members}
    parts = list(
        db.scalars(
            select(EventParticipation).where(
                EventParticipation.id.in_(
                    context.entity_ids(db, uid, room, "PARTICIPATION")
                ),
                EventParticipation.event_id.in_([e.id for e in scoped]),
                EventParticipation.person_id.in_(person_members),
            )
        )
    )
    lower = day_bound(plan.time_start) if plan.time_start else None
    upper = day_bound(min(plan.time_end or plan.as_of, plan.as_of), True)
    if plan.during_event_id:
        interval = db.scalar(event_query.where(Event.id == plan.during_event_id))
        interval_bounds = (
            bounds(TemporalValue.model_validate(interval.occurred_at))
            if interval
            else None
        )
        if interval_bounds is None:
            return result("NO_RECORD", reason="NO_AUTHORIZED_INTERVAL")
        lower = max(lower or interval_bounds[0], interval_bounds[0])
        upper = min(upper, interval_bounds[1])
        interval_entities = [interval.id]
        if plan.during_subject_member_id:
            subject_parts = list(
                db.scalars(
                    select(EventParticipation).where(
                        EventParticipation.id.in_(
                            context.entity_ids(db, uid, room, "PARTICIPATION")
                        ),
                        EventParticipation.event_id == interval.id,
                        EventParticipation.phase_ref == "SUBJECT",
                        EventParticipation.person_id.in_(
                            [
                                p
                                for p, m in person_members.items()
                                if m == plan.during_subject_member_id
                            ]
                        ),
                    )
                )
            )
            if not subject_parts:
                return result("NO_RECORD", reason="NO_AUTHORIZED_INTERVAL_SUBJECT")
            interval_entities.extend(p.id for p in subject_parts)
        interval_sources.extend(sources(db, interval_entities))
    entries, uncertain = [], False
    for event in scoped:
        if (
            plan.related_event_id
            and event.id != plan.related_event_id
            and event.parent_event_id != plan.related_event_id
        ):
            continue
        if plan.title_terms and not any(
            term.casefold() in event.title.casefold() for term in plan.title_terms
        ):
            continue
        if plan.event_phases and event.phase_ref not in plan.event_phases:
            continue
        if plan.compare_gift_use and event.phase_ref not in {
            "GIFT_RECEIVED",
            "GIFT_STORED",
            "GIFT_RETRIEVED",
            "FIRST_USE",
            "USE_CONFIRMED",
        }:
            continue
        participation = [p for p in parts if p.event_id == event.id]
        modes = {}
        for p in participation:
            modes.setdefault(person_members[p.person_id], set()).add(p.mode)
        matched = {
            m for m, values in modes.items() if values & set(plan.participation_modes)
        }
        requested = set(plan.participant_member_ids)
        if requested and not (
            requested <= matched
            if plan.require_all_participants
            else requested & matched
        ):
            continue
        # Subject is explicitly bound via SUBJECT phase, never inferred from
        # onsite attendance, ownership or who uploaded a medical record.
        subjects = {
            person_members[p.person_id]
            for p in participation
            if p.phase_ref == "SUBJECT"
        }
        if plan.subject_member_ids and not subjects.intersection(
            plan.subject_member_ids
        ):
            continue
        if not set(plan.excluded_member_ids) <= {
            m for m, v in modes.items() if "NOT_PRESENT" in v and "ONSITE" not in v
        }:
            continue
        if plan.location and plan.location not in [
            v for v in event.location.values() if isinstance(v, str)
        ]:
            continue
        value = TemporalValue.model_validate(event.occurred_at)
        if plan.time_precisions and value.precision not in plan.time_precisions:
            continue
        span = bounds(value)
        approximate_listing = (
            span is None and plan.operation == "FILTER"
            and not (plan.time_start or plan.time_end or plan.during_event_id or plan.compare_gift_use)
            and value.value is not None and value.value[:4].isdigit()
            and int(value.value[:4]) < plan.as_of.year
        )
        if span is None and not approximate_listing:
            uncertain = True
            continue
        # For gift comparison the service interval constrains receipt only;
        # later storage/retrieval/use observations retain the query as-of bound.
        event_lower, event_upper = lower, upper
        if plan.compare_gift_use and event.phase_ref != "GIFT_RECEIVED":
            event_lower = day_bound(plan.time_start) if plan.time_start else None
            event_upper = day_bound(min(plan.time_end or plan.as_of, plan.as_of), True)
        if span is not None and (span[0] > event_upper or (event_lower and span[1] < event_lower)):
            continue
        if span is not None and (span[1] > event_upper or (event_lower and span[0] < event_lower)):
            uncertain = True
            continue
        if plan.compare_travel_celebration and event.phase_ref not in (
            "TRAVEL_CELEBRATION",
            "NON_TRAVEL_CELEBRATION",
        ):
            return result("CLARIFY", reason="EXPLICIT_EVENT_TYPE_BINDING_REQUIRED")
        linked = (
            sources(db, [event.id, *[p.id for p in participation]]) + interval_sources
        )
        entries.append(
            StructuredEntry(
                event_id=event.id,
                travel_celebration=(event.phase_ref == "TRAVEL_CELEBRATION")
                if plan.compare_travel_celebration
                else None,
                title=event.title,
                event_type=event.category,
                event_status=event.event_status,
                phase=event.phase_ref,
                occurred_at=value,
                location=event.location,
                member_modes={m: sorted(v) for m, v in sorted(modes.items())},
                sources=linked,
            )
        )
    if uncertain:
        return result("CLARIFY", reason="TEMPORAL_UNCERTAINTY")
    if not entries:
        return result("NO_RECORD", reason="NO_AUTHORIZED_MATCH")

    def known_bounds(entry: StructuredEntry) -> tuple[datetime, datetime]:
        span = bounds(entry.occurred_at)
        assert span is not None  # Only bounded entries were admitted above.
        return span

    if plan.operation == "FILTER" and any(bounds(e.occurred_at) is None for e in entries):
        entries.sort(key=lambda e: (e.occurred_at.value or e.occurred_at.start or "", e.event_id))
    else:
        entries.sort(key=lambda e: (*known_bounds(e), e.event_id))
    if plan.operation in ("LATEST", "EARLIEST"):
        winner = entries[-1] if plan.operation == "LATEST" else entries[0]
        span = known_bounds(winner)
        if any(
            e.event_id != winner.event_id
            and not (
                known_bounds(e)[1] < span[0]
                if plan.operation == "LATEST"
                else known_bounds(e)[0] > span[1]
            )
            for e in entries
        ):
            return result("CLARIFY", reason="OVERLAPPING_TIMES")
        entries = [winner]
    elif plan.operation == "TIMELINE" and any(
        known_bounds(a)[1] >= known_bounds(b)[0] for a, b in itertools.pairwise(entries)
    ):
        return result("CLARIFY", reason="OVERLAPPING_TIMES")
    ids = [e.event_id for e in entries]
    groups = {}
    if plan.group_by_member:
        for member in plan.subject_member_ids:
            groups[member] = len(
                {
                    p.event_id
                    for p in parts
                    if p.event_id in ids
                    and person_members[p.person_id] == member
                    and p.phase_ref == "SUBJECT"
                }
            )
    # Never truncate a count's contributing evidence, nor silently omit a timeline.
    if len(entries) > plan.limit:
        return result("CLARIFY", reason="RESULT_LIMIT_REFINE_QUERY")
    gift = {}
    if plan.compare_gift_use:
        received = [e for e in entries if e.phase == "GIFT_RECEIVED"]
        first = [e for e in entries if e.phase == "FIRST_USE"]
        used = [e for e in entries if e.phase == "USE_CONFIRMED"]
        if len(received) != 1 or len(first) > 1:
            return result("CLARIFY", reason="GIFT_IDENTITY_OR_TIME_AMBIGUOUS")
        gift = {
            "received_at": received[0].occurred_at,
            "first_used_at": first[0].occurred_at if first else None,
            "use_confirmed_at": used[0].occurred_at if used else None,
            "reason": None if first else "FIRST_USE_NOT_RECORDED",
        }
    return result(
        "ANSWER",
        **gift,
        entries=entries,
        count=len(ids) if plan.operation == "COUNT" else None,
        grouped_counts=groups,
        member_labels={
            person_members[p.id]: p.display_name
            for p in people
            if p.id in person_members
        },
        contributing_event_ids=ids,
    )


def sources(db, entities):
    return [
        StructuredSource(
            entity_id=s.entity_id,
            record_id=s.record_id,
            record_version=s.record_version,
            source_revision=s.source_revision,
        )
        for s in db.scalars(
            select(ContextSource)
            .where(ContextSource.entity_id.in_(entities))
            .order_by(ContextSource.entity_id, ContextSource.record_id)
        )
    ]


CATEGORIES: dict[str, EventType] = {
    "여행": "TRIP",
    "가족여행": "TRIP",
    "생일": "BIRTHDAY",
    "건강검진": "HEALTH_EXAM",
    "검진": "HEALTH_EXAM",
    "군 복무": "MILITARY",
    "학교": "EDUCATION",
}


def parse(db, uid, room, query, as_of=None):
    """Whole-utterance grammar: unsupported modifiers cannot be discarded."""
    context.membership(db, uid, room)
    q = query.strip().rstrip("?？.").strip()
    natural_count = re.fullmatch(
        r"(.+?)(?:는|은) 최근 (\d{1,2})년(?: 동안)? (건강검진|검진)(?:을|를)? 몇 (번|회) (?:받았어|받았나|받았나요)", q
    )
    if natural_count:
        q = f"최근 {natural_count[2]}년 {natural_count[1]} {natural_count[3]} 몇 {natural_count[4]}"
    day = as_of or datetime.now(ZoneInfo("Asia/Seoul")).date()
    filters = None
    m = re.fullmatch(
        r"(가장 최근|최근|가장 오래된|처음) (가족여행|여행|생일|건강검진|검진|군 복무|학교)(?:은|는)?(?: 언제 어디였나| 언제인가| 언제였나)?",
        q,
    )
    if m:
        filters = StructuredFilters(
            operation="LATEST" if m[1] in ("가장 최근", "최근") else "EARLIEST",
            event_type=CATEGORIES[m[2]],
            as_of=day,
        )
    m_attendees = re.fullmatch(
        r"(?:가장 최근|최근) (가족여행|여행)에 누가 같이 갔나", q
    )
    if m_attendees:
        filters = StructuredFilters(
            operation="LATEST", event_type="TRIP", include_participants=True, as_of=day
        )
    m_total = re.fullmatch(
        r"(가족여행|여행|검진|건강검진|생일)(?:은|는)? 몇 (?:번|회)(?:인가)?", q
    )
    if m_total:
        filters = StructuredFilters(
            operation="COUNT", event_type=CATEGORIES[m_total[1]], as_of=day
        )
    m_all = re.fullmatch(
        r"(네 명|모두|가족 모두)(?: 모두)?(?:가)? 함께한 가장 최근 여행(?:은)?", q
    )
    if m_all:
        ids = list(
            db.scalars(
                select(FamilyMember.id).where(
                    FamilyMember.room_id == room, FamilyMember.is_active.is_(True)
                )
            )
        )
        if m_all[1] == "네 명" and len(ids) != 4:
            return StructuredResult(route="CLARIFY", reason="MEMBER_SET_AMBIGUOUS")
        filters = StructuredFilters(
            operation="LATEST",
            event_type="TRIP",
            participant_member_ids=ids,
            require_all_participants=True,
            as_of=day,
        )
    m_count = re.fullmatch(
        r"최근 (\d{1,2})년 (.+?) (검진|건강검진)(?:은|는)? (각각 )?몇 (?:번|회)(?:인가)?",
        q,
    )
    if m_count:
        labels = (
            ["엄마", "아빠"]
            if m_count[2] == "부모"
            else re.split(r"와 |과 |,\s*", m_count[2])
        )
        ids = []
        for label in labels:
            resolution = context.resolve(db, uid, room, label, day)
            if (
                resolution.status != "RESOLVED"
                or len(resolution.resolved_member_ids) != 1
            ):
                return StructuredResult(route="CLARIFY", reason="MEMBER_SET_AMBIGUOUS")
            ids.extend(resolution.resolved_member_ids)
        years = int(m_count[1])
        if not 1 <= years <= 50 or day.year - years < 2:
            return StructuredResult(
                route="UNSUPPORTED", reason="TIME_WINDOW_UNSUPPORTED"
            )
        start = date(
            day.year - years,
            day.month,
            min(day.day, calendar.monthrange(day.year - years, day.month)[1]),
        ) + timedelta(days=1)
        filters = StructuredFilters(
            operation="COUNT",
            event_type="HEALTH_EXAM",
            subject_member_ids=sorted(set(ids)),
            group_by_member=True,
            time_start=start,
            time_end=day,
            as_of=day,
        )
    m_service = re.fullmatch(
        r"(.+?)(?:이|가)? 군 복무 중 (?:갔다고 잘못 말하면 안 되는|참석하지 않은|가지 않은) 여행(?:은)?",
        q,
    )
    if m_service:
        resolution = context.resolve(db, uid, room, m_service[1], day)
        if resolution.status != "RESOLVED" or len(resolution.resolved_member_ids) != 1:
            return StructuredResult(route="CLARIFY", reason="MEMBER_SET_AMBIGUOUS")
        service_events = list(
            db.scalars(
                select(Event).where(
                    Event.id.in_(context.entity_ids(db, uid, room, "EVENT")),
                    Event.category == "MILITARY",
                    Event.phase_ref == "SERVICE",
                    Event.id.in_(
                        select(EventParticipation.event_id).where(
                            EventParticipation.id.in_(
                                context.entity_ids(db, uid, room, "PARTICIPATION")
                            ),
                            EventParticipation.person_id
                            == resolution.resolved_person_id,
                            EventParticipation.phase_ref == "SUBJECT",
                        )
                    ),
                )
            )
        )
        if len(service_events) != 1:
            return StructuredResult(
                route="CLARIFY" if service_events else "NO_RECORD",
                reason="SERVICE_INTERVAL_REQUIRED",
            )
        filters = StructuredFilters(
            operation="TIMELINE",
            event_type="TRIP",
            excluded_member_ids=resolution.resolved_member_ids,
            during_event_id=service_events[0].id,
            during_subject_member_id=resolution.resolved_member_ids[0],
            as_of=day,
        )
    m_health = re.fullmatch(r"(.+?)(?:는|은) 어떤 과정을 거쳐 건강 상태가 달라졌나", q)
    m_condition = re.fullmatch(
        r"(.+?)의 ((?:건강 상태|혈압|고혈압|피로|빈혈)(?:(?:와|과) (?:혈압|고혈압|피로|빈혈))?)(?:은|는) 어떻게 이어졌나",
        q,
    )
    if m_health or m_condition:
        match = m_health or m_condition
        assert match is not None
        resolved = context.resolve(db, uid, room, match[1], day)
        if resolved.status != "RESOLVED" or len(resolved.resolved_member_ids) != 1:
            return StructuredResult(route="CLARIFY", reason="MEMBER_SET_AMBIGUOUS")
        terms = re.split(r"와 |과 ", m_condition[2]) if m_condition else []
        filters = StructuredFilters(
            operation="TIMELINE",
            subject_member_ids=resolved.resolved_member_ids,
            event_types=["HEALTH_EXAM", "HEALTH_REPORT", "DIAGNOSIS"],
            title_terms=[] if terms == ["건강 상태"] else terms,
            as_of=day,
        )
    m_gift = re.fullmatch(r"군 복무 중 생일 선물 (.+?)(?:은|는) 언제 실제 사용했나", q)
    if m_gift:
        # Resolve one visible source-bound gift and its explicit subject first.
        receipt_plan = plan_for(
            db,
            uid,
            room,
            StructuredFilters(
                operation="FILTER",
                event_phases=["GIFT_RECEIVED"],
                title_terms=[m_gift[1]],
                as_of=day,
            ),
        )
        receipts = execute(db, uid, room, receipt_plan)
        if receipts.route != "ANSWER":
            return receipts
        if len(receipts.entries) != 1:
            return StructuredResult(route="CLARIFY", reason="GIFT_IDENTITY_AMBIGUOUS")
        receipt = db.get(Event, receipts.entries[0].event_id)
        subject_people = set(
            db.scalars(
                select(EventParticipation.person_id).where(
                    EventParticipation.id.in_(
                        context.entity_ids(db, uid, room, "PARTICIPATION")
                    ),
                    EventParticipation.event_id == receipt.id,
                    EventParticipation.phase_ref == "SUBJECT",
                )
            )
        )
        people = [
            p
            for p in db.scalars(context.visible_people(db, room))
            if p.id in subject_people
        ]
        subject_members = list(
            db.scalars(
                select(FamilyMember.id).where(
                    FamilyMember.room_id == room,
                    FamilyMember.is_active.is_(True),
                    FamilyMember.user_id.in_([p.user_id for p in people]),
                )
            )
        )
        if len(subject_members) != 1:
            return StructuredResult(route="CLARIFY", reason="GIFT_SUBJECT_REQUIRED")
        service = execute(
            db,
            uid,
            room,
            plan_for(
                db,
                uid,
                room,
                StructuredFilters(
                    operation="FILTER",
                    event_type="MILITARY",
                    event_phases=["SERVICE"],
                    subject_member_ids=subject_members,
                    as_of=day,
                ),
            ),
        )
        if service.route != "ANSWER":
            return service
        if len(service.entries) != 1:
            return StructuredResult(route="CLARIFY", reason="SERVICE_INTERVAL_REQUIRED")
        filters = StructuredFilters(
            operation="FILTER",
            subject_member_ids=subject_members,
            related_event_id=receipt.id,
            title_terms=[m_gift[1]],
            event_phases=[
                "GIFT_RECEIVED",
                "GIFT_STORED",
                "GIFT_RETRIEVED",
                "FIRST_USE",
                "USE_CONFIRMED",
            ],
            compare_gift_use=True,
            during_event_id=service.entries[0].event_id,
            during_subject_member_id=subject_members[0],
            as_of=day,
        )
    m_life = re.fullmatch(
        r"(.+?)(?:의)? 대학 입학부터 군 복무와 복학까지 어떻게 이어졌나", q
    )
    if m_life:
        resolved = context.resolve(db, uid, room, m_life[1], day)
        if resolved.status != "RESOLVED" or len(resolved.resolved_member_ids) != 1:
            return StructuredResult(route="CLARIFY", reason="MEMBER_SET_AMBIGUOUS")
        filters = StructuredFilters(
            operation="TIMELINE",
            subject_member_ids=resolved.resolved_member_ids,
            event_types=["EDUCATION", "MILITARY"],
            event_phases=[
                "UNIVERSITY_ENTRY",
                "ENLISTMENT",
                "DISCHARGE",
                "UNIVERSITY_RETURN",
            ],
            as_of=day,
        )
    m_birthday = re.fullmatch(r"(\d{4})년 (.+?) 생일은 여행으로 축하했나", q)
    if m_birthday:
        year = int(m_birthday[1])
        if not 2 <= year <= 9998:
            return StructuredResult(
                route="UNSUPPORTED", reason="TIME_WINDOW_UNSUPPORTED"
            )
        ids = []
        for label in re.split(r"와 |과 |,\s*", m_birthday[2]):
            resolved = context.resolve(db, uid, room, label, day)
            if resolved.status != "RESOLVED" or len(resolved.resolved_member_ids) != 1:
                return StructuredResult(route="CLARIFY", reason="MEMBER_SET_AMBIGUOUS")
            ids.extend(resolved.resolved_member_ids)
        filters = StructuredFilters(
            operation="FILTER",
            event_type="BIRTHDAY",
            subject_member_ids=ids,
            time_start=date(year, 1, 1),
            time_end=date(year, 12, 31),
            as_of=day,
            compare_travel_celebration=True,
        )
    m_timeline = re.fullmatch(
        r"(.+?) (여행|생일|검진|건강검진|군 복무|학교)(?:은|는|이|가)? (?:언제부터 언제까지|어떻게 이어졌나|연표)",
        q,
    )
    m_part = re.fullmatch(
        r"(.+?)(?:이|가)? (가지 않은|참석하지 않은|실제 참여한|참여한) (여행|생일|학교)(?:은|는)?",
        q,
    )
    match = m_timeline or m_part
    if match:
        resolution = context.resolve(db, uid, room, match[1], day)
        if resolution.status != "RESOLVED" or len(resolution.resolved_member_ids) != 1:
            return StructuredResult(route="CLARIFY", reason="MEMBER_SET_AMBIGUOUS")
        ids = resolution.resolved_member_ids
        negative = bool(m_part and m_part[2] in ("가지 않은", "참석하지 않은"))
        filters = StructuredFilters(
            operation="TIMELINE" if m_timeline else "FILTER",
            event_type=CATEGORIES[match[2] if m_timeline else match[3]],
            participant_member_ids=[] if negative else ids,
            excluded_member_ids=ids if negative else [],
            participation_modes=["ONSITE", "REMOTE", "SUPPORT", "MENTIONED"]
            if m_part and m_part[2] == "참여한"
            else ["ONSITE"],
            as_of=day,
        )
    if filters is None:
        # Unrecognized structured language must never fall back to LLM aggregation.
        if re.search(
            r"최근|오래된|처음|몇 번|몇 회|모두|참석|가지 않은|군 복무 중|이어졌|언제부터|여행으로 축하|누가 같이|건강 상태가 달라졌",
            q,
        ):
            return StructuredResult(route="UNSUPPORTED", reason="UNSUPPORTED_GRAMMAR")
        return None
    return plan_for(db, uid, room, filters)


def ask_query(db, uid, room, query, filters=None, as_of=None):
    started = perf_counter()
    if filters is not None and filters.as_of is None and as_of is not None:
        filters = filters.model_copy(update={"as_of": as_of})
    plan = (
        plan_for(db, uid, room, filters)
        if filters is not None
        else parse(db, uid, room, query, as_of)
    )
    elapsed = (perf_counter() - started) * 1000
    if plan is None:
        return None
    result = (
        plan if isinstance(plan, StructuredResult) else execute(db, uid, room, plan)
    )
    result.planner_ms = elapsed
    return result
