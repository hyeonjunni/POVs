"""Bounded M5.1 claim payloads, stored only inside encrypted room components."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.family_context import TemporalValue


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimSource(StrictModel):
    record_id: str
    record_version: int = Field(ge=1)
    source_revision: int = Field(ge=1)
    chunk_id: str
    file_id: str
    processing_job_id: str
    source_attempt: int = Field(ge=0)
    source_result_sha256: str
    text_sha256: str
    source_object_id: str
    source_object_version: int = Field(ge=1)
    source_ciphertext_sha256: str
    source_locator_json: str
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)


class Proposition(StrictModel):
    subject_id: str
    predicate: Literal[
        "ACTION",
        "EVENT_OCCURRENCE",
        "MEMORY_STATE",
        "MOTIVE",
        "EMOTION",
        "PARTICIPATION",
        "ATTRIBUTE",
    ]
    dimension: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)
    polarity: Literal["AFFIRMED", "NEGATED", "UNCERTAIN"] = "AFFIRMED"
    # Reviewed single-valued semantics, never inferred from different strings.
    exclusive: bool = False
    temporal_scope: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def memory_values(self):
        if self.predicate == "MEMORY_STATE" and (
            self.value
            not in {
                "REMEMBERS",
                "FAINT",
                "DOES_NOT_REMEMBER",
                "HEARD_FROM_OTHER",
                "UNKNOWN",
            }
            or self.polarity != "AFFIRMED"
        ):
            raise ValueError("memory absence is an affirmative memory-state claim")
        return self


class ClaimPayload(StrictModel):
    claim_id: str
    claim_version: int = Field(ge=1)
    room_id: str
    event_id: str | None = None
    source: ClaimSource
    claimant_member_id: str | None = None
    subject_member_id: str | None = None
    source_narrator_member_id: str | None = None
    claimant_label: str = Field(min_length=1, max_length=128)
    epistemic_basis: Literal[
        "DIRECT_EXPERIENCE",
        "RETROSPECTIVE_MEMORY",
        "HEARD_FROM_OTHER",
        "DOCUMENTED_SOURCE",
        "INTERPRETATION",
        "UNKNOWN",
    ]
    perspective_type: Literal[
        "OBSERVATION",
        "DIRECT_MEMORY",
        "RECOLLECTION",
        "REPORTED",
        "INTERPRETATION",
        "DOCUMENT",
    ]
    proposition: Proposition
    quote: str = Field(min_length=1, max_length=4096)
    certainty: Literal["CERTAIN", "PROBABLE", "POSSIBLE", "UNSPECIFIED"] = "UNSPECIFIED"
    event_occurred_at: TemporalValue | None = None
    recorded_at: TemporalValue | None = None
    recollection_source_date: TemporalValue | None = None
    # Explicit reviewed statement, never inferred from a photo/behavior or interpretation.
    explicit_mental_state: bool = False
    supersedes_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def consistent(self):
        if (self.epistemic_basis == "INTERPRETATION") != (
            self.perspective_type == "INTERPRETATION"
        ):
            raise ValueError("interpretation must remain explicitly labeled")
        if (
            self.proposition.predicate == "MOTIVE"
            and self.epistemic_basis != "INTERPRETATION"
        ):
            raise ValueError("M5.1 motive claims require interpretation labeling")
        if self.proposition.predicate == "EMOTION" and (
            not self.subject_member_id
            or self.proposition.subject_id != self.subject_member_id
        ):
            raise ValueError("emotion requires its explicit member subject")
        if self.proposition.predicate == "MEMORY_STATE":
            if (
                not self.subject_member_id
                or self.proposition.subject_id != self.subject_member_id
            ):
                raise ValueError("memory state requires its explicit member subject")
            if not self.event_id and not self.proposition.temporal_scope:
                raise ValueError("memory state requires event or proposition scope")
            if self.epistemic_basis in {"DOCUMENTED_SOURCE", "INTERPRETATION"}:
                raise ValueError(
                    "document appearance or interpretation cannot establish memory"
                )
        if self.explicit_mental_state and (
            self.proposition.predicate != "EMOTION"
            or self.epistemic_basis in {"INTERPRETATION", "UNKNOWN"}
        ):
            raise ValueError("explicit emotion requires a reviewed statement basis")
        if self.claim_version == 1 and self.supersedes_version is not None:
            raise ValueError("first version has no predecessor")
        if self.claim_version > 1 and self.supersedes_version != self.claim_version - 1:
            raise ValueError("explicit contiguous version lineage required")
        if self.source.quote_end - self.source.quote_start != len(self.quote):
            raise ValueError("exact source span required")
        return self


class ClaimQuery(StrictModel):
    operation: Literal[
        "PERSPECTIVE_COMPARE",
        "CONFLICT",
        "CLAIM_TIMELINE",
        "MEMORY_STATE",
        "PARTICIPATION_MEMORY_COMPARE",
        "MENTAL_STATE_QUERY",
    ] = "PERSPECTIVE_COMPARE"
    event_id: str | None = None
    subject_member_ids: list[str] = Field(default_factory=list, max_length=20)
    emotion: Literal["AFRAID", "HAPPY", "EXCITED", "SAD", "NERVOUS", "ANGRY"] | None = (
        None
    )
    claimant_member_ids: list[str] = Field(default_factory=list, max_length=20)


class ClaimReview(StrictModel):
    claim_version: int = Field(ge=1)
    status: Literal["PENDING_REVIEW", "APPROVED", "REJECTED"]


class ClaimSubmission(StrictModel):
    payload: ClaimPayload
    status: Literal["PREDICTED", "PENDING_REVIEW"] = "PENDING_REVIEW"
