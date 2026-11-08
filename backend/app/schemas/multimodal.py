"""M4 additive, strict reviewed-media contracts; model output has no identity fields."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.family_context import TemporalValue


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisualPrediction(Closed):
    scene: str = Field(default="", max_length=4000)
    objects: list[str] = Field(default_factory=list, max_length=100)
    activities: list[str] = Field(default_factory=list, max_length=100)
    people_count: int | None = Field(default=None, ge=0, le=1000)
    generic_people_descriptions: list[str] = Field(default_factory=list, max_length=100)
    location_hint: str | None = Field(default=None, max_length=2000)
    visual_description: str = Field(default="", max_length=16000)
    candidate_event: str | None = Field(default=None, max_length=4000)


class Segment(Closed):
    segment_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(max_length=32000)

    @model_validator(mode="after")
    def bounds(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("Segment must have a positive interval")
        return self


class FrameAnnotation(Closed):
    frame_id: UUID
    timestamp_ms: int = Field(ge=0)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ocr_outcome: Literal["OCR_TEXT_FOUND", "OCR_EMPTY_SUCCESS", "OCR_PROCESSING_ERROR"]
    ocr_text: str = Field(default="", max_length=32000)
    visual: VisualPrediction | None = None


class Annotations(Closed):
    ocr_text: str = Field(default="", max_length=100000)
    visual: VisualPrediction | None = None
    transcript: str = Field(default="", max_length=200000)
    language: str = Field(default="und", max_length=32)
    segments: list[Segment] = Field(default_factory=list, max_length=2000)
    frames: list[FrameAnnotation] = Field(default_factory=list, max_length=12)


class TrustedMetadata(Closed):
    """Accepted only from an authorized reviewer, never an extraction provider."""
    speaker_member_id: UUID | None = None
    visible_member_ids: list[UUID] = Field(default_factory=list, max_length=100)
    event_id: UUID | None = None
    recorded_at: TemporalValue | None = None
    event_occurred_at: TemporalValue | None = None


class MediaReviewRequest(Closed):
    action: Literal["approve", "correct", "reject"]
    source_attempt: int = Field(ge=0)
    source_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_version: int = Field(ge=0)
    approved: Annotations | None = None
    metadata: TrustedMetadata = Field(default_factory=TrustedMetadata)
    excluded_stages: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def correction(self):
        if (self.action == "correct") != (self.approved is not None):
            raise ValueError("Only correction supplies approved annotations")
        return self


class MediaAnchor(Closed):
    kind: Literal["IMAGE", "AUDIO", "VIDEO"]
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    field: Literal["ocr", "visual"] | None = None
    segment_id: UUID | None = None
    frame_id: UUID | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    frame_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def anchor(self):
        if self.char_end <= self.char_start:
            raise ValueError("Invalid anchor")
        if self.kind == "AUDIO" and (self.segment_id is None or self.start_ms is None
                or self.end_ms is None or self.end_ms <= self.start_ms):
            raise ValueError("Invalid audio anchor")
        if self.kind == "VIDEO" and (self.frame_id is None or self.frame_ms is None):
            raise ValueError("Invalid video anchor")
        return self


class MediaLocator(Closed):
    kind: Literal["multimodal"]
    media_type: Literal["IMAGE", "AUDIO", "VIDEO"]
    review_version: int = Field(ge=1)
    anchors: list[MediaAnchor] = Field(min_length=1, max_length=2012)
