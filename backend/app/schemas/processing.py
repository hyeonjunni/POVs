from typing import Literal

from pydantic import BaseModel, ConfigDict


class ProcessingJobCreateRequest(BaseModel):
    upload_file_id: str
    processor: str  # "ocr" | "stt" | "text"


class ProcessingJobResponse(BaseModel):
    job_id: str
    record_id: str
    room_id: str
    upload_file_id: str
    processor: str
    status: str
    attempt: int
    result: dict | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class OCRReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject"]


class OCRReviewResponse(BaseModel):
    job_id: str
    review_status: Literal["unreviewed", "confirmed", "rejected"]
    user_confirmed: bool
    source_attempt: int
    source_result_sha256: str
