"""M3 additive DTOs. Dataset precision never changes existing v1 time enums."""
import re
from datetime import date, datetime
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

Precision = Literal["EXACT", "RANGE", "MONTH", "SEASON", "YEAR", "APPROXIMATE", "UNKNOWN"]
ParticipationMode = Literal["ONSITE", "REMOTE", "SUPPORT", "MENTIONED", "NOT_PRESENT"]
EventStatus = Literal["PLANNED", "ONGOING", "COMPLETED", "CANCELLED", "UNKNOWN"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemporalValue(StrictModel):
    precision: Precision
    start: str | None = None
    end: str | None = None
    value: str | None = None
    granularity: Literal["INSTANT", "DAY", "MONTH", "SEASON", "YEAR", "UNKNOWN"] = "UNKNOWN"
    timezone: str = "Asia/Seoul"
    original_text: str | None = None
    qualification: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid timezone") from exc
        if self.precision == "UNKNOWN":
            if any(x is not None for x in (self.value, self.start, self.end)) or self.granularity != "UNKNOWN":
                raise ValueError("UNKNOWN must not contain inferred bounds")
            return self
        if self.precision == "APPROXIMATE" and not (self.original_text and self.qualification):
            raise ValueError("approximation requires source wording and qualification")
        if self.precision == "RANGE":
            if self.value is not None or self.start is None or self.end is None:
                raise ValueError("range requires two bounds, not value")
        elif (self.start is not None or self.end is not None) and (self.precision != "APPROXIMATE" or self.value is not None):
            raise ValueError("only qualified approximations may retain source-supplied bounds")
        if self.precision != "APPROXIMATE" and self.precision != "RANGE" and self.value is None:
            raise ValueError("known precision requires value")
        expected = {"MONTH": "MONTH", "YEAR": "YEAR", "SEASON": "SEASON"}.get(self.precision)
        if expected and self.granularity != expected:
            raise ValueError("precision/granularity mismatch")
        if self.precision == "EXACT" and self.granularity not in ("DAY", "INSTANT"):
            raise ValueError("EXACT requires DAY or INSTANT")
        parsed = []
        for value in (self.value, self.start, self.end):
            if value is None:
                continue
            if self.granularity == "DAY":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError("invalid day")
                parsed.append(date.fromisoformat(value))
            elif self.granularity == "INSTANT":
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ValueError("instant requires offset")
                parsed.append(instant)
            elif self.granularity == "MONTH":
                if not re.fullmatch(r"\d{4}-\d{2}", value):
                    raise ValueError("invalid month")
                date.fromisoformat(value + "-01")
                parsed.append(value)
            elif self.granularity == "YEAR":
                if not re.fullmatch(r"\d{4}", value) or not 1 <= int(value) <= 9999:
                    raise ValueError("invalid year")
                parsed.append(value)
            elif self.granularity == "SEASON":
                if not re.fullmatch(r"\d{4}-(SPRING|SUMMER|AUTUMN|WINTER)", value):
                    raise ValueError("invalid season")
                parsed.append(value)
            else:
                raise ValueError("unknown granularity cannot carry a value")
        if self.start is not None and self.end is not None and (self.granularity == "SEASON" or parsed[0] > parsed[1]):
            raise ValueError("invalid/reversed range")
        return self

    def latest_known_day(self) -> date | None:
        """Only exact day/instant bounds; no manufactured season boundary."""
        if self.precision not in ("EXACT", "RANGE"):
            return None
        value = self.end if self.precision == "RANGE" else self.value
        if value is None:
            return None
        if self.granularity == "DAY":
            return date.fromisoformat(value)
        if self.granularity == "INSTANT":
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(self.timezone)).date()
        return None


class ResolutionRequest(StrictModel):
    surface: str = Field(min_length=1, max_length=200)
    as_of: date | None = None


class Resolution(StrictModel):
    expression: str
    status: Literal["RESOLVED", "AMBIGUOUS", "UNRESOLVED"]
    resolved_person_id: str | None
    candidate_person_ids: list[str]
    resolved_member_ids: list[str]
    speaker_person_id: str | None
    basis: Literal["EXACT_NAME", "APPROVED_ALIAS"]
    clarification: str | None
    as_of: date


class ContextFilters(StrictModel):
    resolved_member_ids: list[str] = Field(default_factory=list)
    subject_member_ids: list[str] = Field(default_factory=list)
    participant_member_ids: list[str] = Field(default_factory=list)
    excluded_member_ids: list[str] = Field(default_factory=list)
    event_type: str | None = None
    time_constraint: TemporalValue | None = None
    location: str | None = None
    media_type: Literal["TEXT", "IMAGE", "AUDIO", "VIDEO", "DOCUMENT"] | None = None
    participation_requirement: ParticipationMode | None = None
    event_status: EventStatus | None = None
    as_of: date | None = None


class QueryContext(ContextFilters):
    viewer_member_id: str
    family_room_id: str
