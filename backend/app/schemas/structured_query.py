"""Bounded M6.1 plans and results; no expressions or client-supplied viewer."""

from datetime import date
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from app.schemas.family_context import (
    EventStatus,
    ParticipationMode,
    Precision,
    StrictModel,
    TemporalValue,
)

Operation = Literal["FILTER", "LATEST", "EARLIEST", "COUNT", "TIMELINE"]
EventType = Literal[
    "TRIP",
    "BIRTHDAY",
    "HEALTH_EXAM",
    "HEALTH_REPORT",
    "DIAGNOSIS",
    "EDUCATION",
    "MILITARY",
    "FAMILY_ACTIVITY",
    "WORK",
    "OTHER",
]


class _StructuredFields(StrictModel):
    operation: Operation
    subject_member_ids: list[str] = Field(default_factory=list, max_length=50)
    participant_member_ids: list[str] = Field(default_factory=list, max_length=50)
    excluded_member_ids: list[str] = Field(default_factory=list, max_length=50)
    event_type: EventType | None = None
    event_types: list[EventType] = Field(default_factory=list, max_length=10)
    event_phases: list[
        Literal[
            "UNIVERSITY_ENTRY",
            "ENLISTMENT",
            "DISCHARGE",
            "UNIVERSITY_RETURN",
            "CHECKUP",
            "RESULT",
            "DIAGNOSIS",
            "FOLLOWUP",
            "GIFT_RECEIVED",
            "GIFT_STORED",
            "GIFT_RETRIEVED",
            "USE_CONFIRMED",
            "FIRST_USE",
            "SERVICE",
            "TRAVEL_CELEBRATION",
            "NON_TRAVEL_CELEBRATION",
        ]
    ] = Field(default_factory=list)
    compare_travel_celebration: bool = False
    # Literal source-backed title filters, not model-derived condition claims.
    title_terms: list[str] = Field(default_factory=list, max_length=10)
    related_event_id: str | None = None
    compare_gift_use: bool = False
    event_status: EventStatus = "COMPLETED"
    participation_modes: list[ParticipationMode] = Field(
        default_factory=lambda: ["ONSITE"], min_length=1
    )
    require_all_participants: bool = False
    group_by_member: bool = False
    include_participants: bool = False
    time_start: date | None = Field(
        default=None, ge=date(2, 1, 1), le=date(9998, 12, 31)
    )
    time_end: date | None = Field(default=None, ge=date(2, 1, 1), le=date(9998, 12, 31))
    time_precisions: list[Precision] = Field(default_factory=list)
    location: str | None = Field(default=None, max_length=200)
    # A source-bound interval event (e.g. military service), never a prose inference.
    during_event_id: str | None = None
    during_subject_member_id: str | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if any(not term.strip() or len(term) > 200 for term in self.title_terms):
            raise ValueError("title terms must be nonempty and bounded")
        if self.compare_gift_use:
            if self.operation != "FILTER" or not self.related_event_id:
                raise ValueError("gift comparison requires FILTER and one receipt root")
            if len(self.subject_member_ids) != 1:
                raise ValueError("gift comparison requires one explicit subject")
            phases = {
                "GIFT_RECEIVED",
                "GIFT_STORED",
                "GIFT_RETRIEVED",
                "FIRST_USE",
                "USE_CONFIRMED",
            }
            if self.event_phases and (
                "GIFT_RECEIVED" not in self.event_phases
                or not set(self.event_phases) <= phases
            ):
                raise ValueError(
                    "gift comparison requires receipt and only gift phases"
                )
        if self.during_subject_member_id and not self.during_event_id:
            raise ValueError("interval subject requires interval event")
        if self.time_start and self.time_end and self.time_start > self.time_end:
            raise ValueError("reversed time window")
        if set(self.participant_member_ids) & set(self.excluded_member_ids):
            raise ValueError("contradictory participation")
        if self.group_by_member and (
            self.operation != "COUNT" or not self.subject_member_ids
        ):
            raise ValueError("grouped count requires subjects")
        if self.event_type and self.event_types:
            raise ValueError("use one event-type filter")
        return self


class StructuredFilters(_StructuredFields):
    as_of: date | None = Field(default=None, ge=date(2, 1, 1), le=date(9998, 12, 31))


class StructuredPlan(_StructuredFields):
    viewer_member_id: str
    family_room_id: str
    as_of: date = Field(ge=date(2, 1, 1), le=date(9998, 12, 31))


class StructuredSource(StrictModel):
    entity_id: str
    record_id: str
    record_version: int
    source_revision: int


class StructuredEntry(StrictModel):
    event_id: str
    title: str
    event_type: str
    event_status: EventStatus
    phase: str | None = None
    occurred_at: TemporalValue
    location: dict[str, Any]
    travel_celebration: bool | None = None
    member_modes: dict[str, list[ParticipationMode]]
    sources: list[StructuredSource]


class StructuredResult(StrictModel):
    route: Literal["ANSWER", "NO_RECORD", "CLARIFY", "UNSUPPORTED"]
    plan: StructuredPlan | None = None
    entries: list[StructuredEntry] = Field(default_factory=list)
    received_at: TemporalValue | None = None
    first_used_at: TemporalValue | None = None
    use_confirmed_at: TemporalValue | None = None
    count: int | None = None
    member_labels: dict[str, str] = Field(default_factory=dict)
    grouped_counts: dict[str, int] = Field(default_factory=dict)
    # Exact contributor identity is retained independently of display limit.
    contributing_event_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    planner_ms: float = 0
    execution_ms: float = 0
    rendering_ms: float = 0
