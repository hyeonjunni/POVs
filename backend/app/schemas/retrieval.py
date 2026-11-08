from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RetrievalArm = Literal["substring", "bm25", "vector", "hybrid_rrf"]


class RetrievalSearchRequest(BaseModel):
    query: str
    arm: RetrievalArm
    k: int = Field(ge=1, le=50)


class RetrievalHitResponse(BaseModel):
    chunk_id: str
    record_id: str
    file_id: str
    text: str
    source_locator: dict
    rank: int
    scores: dict[str, float]


class RetrievalSearchResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    arm: RetrievalArm
    model_key: str | None = None
    hits: list[RetrievalHitResponse]


class RetrievalIndexRequest(BaseModel):
    processing_job_id: str


class RetrievalIndexResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    source_attempt: int
    source_result_sha256: str
    chunk_count: int
    embedding_count: int
    chunker_version: str
    model_key: str
