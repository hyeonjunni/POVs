from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.upload import UploadCreateRequest


class RecordCreateRequest(UploadCreateRequest):
    model_config = ConfigDict(extra="forbid")
    visibility: Literal["PRIVATE", "FAMILY", "CUSTOM"] = "PRIVATE"
    custom_grants: list[str] = Field(default_factory=list)
    subject_member_ids: list[str] = Field(default_factory=list)
    participant_member_ids: list[str] = Field(default_factory=list)
    perspective_member_id: str | None = None


class RecordAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: str = Field(min_length=1, max_length=2000)


class LifecycleTransition(RecordAction):
    action: Literal["HIDE", "ARCHIVE", "RESTORE", "SOFT_DELETE", "ACTIVATE"]


class VisibilityChange(RecordAction):
    visibility: Literal["PRIVATE", "FAMILY", "CUSTOM"]
    custom_grants: list[str] = Field(default_factory=list)


class VisibilityReduction(RecordAction):
    allowed_member_ids: list[str] = Field(default_factory=list)


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    document_name: str
    source_section: str
    synthetic: bool
    classification_reason: str | None = None


class ImageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    caption: str | None = None
    generation_prompt: str | None = None
    transcription_gold: str | None = None
    fictional_archive_description: str | None = None
    source_record_refs: list[str] | None = None
    ownership_basis: str | None = None


class AudioMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    transcript_gold: str | None = None
    recording_context: str | None = None


class MetadataChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str | None = None
    subject_member_ids: list[str] | None = None
    participant_member_ids: list[str] | None = None
    occurred_at: str | None = None
    location: str | None = None
    event_ids: list[str] | None = None
    category: str | None = None
    source_record_id: str | None = None
    authored_at: str | None = None
    record_kind: Literal["PERSONAL", "SHARED", "EVENT"] | None = None
    source_provenance: SourceProvenance | None = None
    image_metadata: ImageMetadata | None = None
    audio_metadata: AudioMetadata | None = None


class MetadataPatch(RecordAction):
    changes: MetadataChanges


class DeletionDecision(RecordAction):
    decision: Literal["ACCEPT", "DECLINE", "WITHDRAW"]


class PurgeRequest(RecordAction):
    confirmation: Literal["PERMANENTLY_DELETE"]
