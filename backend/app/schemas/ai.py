from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from app.schemas.claims import ClaimQuery
from app.schemas.retrieval import RetrievalArm
from app.schemas.structured_query import StructuredFilters, StructuredResult


class AskTurn(BaseModel):
    query: str = Field(min_length=1, max_length=4096)
    record_ids: list[UUID] = Field(default_factory=list, max_length=20)


class AskRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    query: str = Field(min_length=1, max_length=4096)
    model_id: str = Field(min_length=1, max_length=128)
    arm: RetrievalArm
    k: int = Field(ge=1, le=50)
    as_of: date | None = Field(default=None, ge=date(2, 1, 1), le=date(9998, 12, 31))
    structured_query: StructuredFilters | None = None
    claim_query: ClaimQuery | None = None
    history: list[AskTurn] = Field(default_factory=list, max_length=4)
    selected_record_id: UUID | None = None
    context_mode: Literal["AUTO", "ROOM", "FOLLOWUP"] = "AUTO"

    @model_validator(mode='after')
    def consistent_as_of(self):
        if self.claim_query is not None and self.structured_query is not None:
            raise ValueError('choose one structured or claim query')
        if self.as_of and self.structured_query and self.structured_query.as_of and self.as_of != self.structured_query.as_of:
            raise ValueError('conflicting as_of values')
        return self

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must not be blank")
        return value


class AskCitation(BaseModel):
    fact_id: str
    chunk_id: str
    record_id: str
    file_id: str
    chunk_char_start: int
    chunk_char_end: int
    source_locator: dict[str, object]
    extractor: str
    extractor_id: str
    review_status: str
    user_confirmed: bool
    evidence_status: Literal["extracted"] = "extracted"


class AnswerContextRecord(BaseModel):
    record_id: UUID
    title: str
    record_type: str | None = None
    record_kind: str | None = None


class AnswerContext(BaseModel):
    scope: Literal["ROOM", "FOLLOWUP", "RECORD"]
    records: list[AnswerContextRecord]
    previous_query: str | None = None
    viewer_name: str | None = None
    address_bindings: list[dict[str, str]] = Field(default_factory=list)
    personal_memory_applied: bool = False


class AskResponse(BaseModel):
    gate_route: Literal["ANSWER", "REFUSE", "CLARIFY", "CONFLICT", "NO_RECORD", "NEEDS_DATA"]
    answer: str | None
    citations: list[AskCitation]
    gate_action: str
    provider_mode: str
    structured_result: StructuredResult | None = None
    claim_result: dict | None = None
    query_context: AnswerContext | None = None
    general_sources: list[str] = Field(default_factory=list, max_length=10)

    @model_serializer(mode='wrap')
    def preserve_direct_response(self, handler):
        value = handler(self)
        if self.structured_result is None:
            value.pop('structured_result', None)
        if self.claim_result is None:
            value.pop('claim_result', None)
        if self.query_context is None:
            value.pop('query_context', None)
        if not self.general_sources:
            value.pop('general_sources', None)
        return value


class SecureAskCitation(AskCitation):
    claim_id: str | None = None
    claim_version: int | None = None
    claim_quote_start: int | None = None
    claim_quote_end: int | None = None

    @model_serializer(mode='wrap')
    def preserve_source_citation(self, handler):
        value = handler(self)
        if self.claim_id is None:
            for key in ("claim_id", "claim_version", "claim_quote_start", "claim_quote_end"):
                value.pop(key, None)
        return value

    source_object_id: str
    source_object_version: int
    source_ciphertext_sha256: str
    original_url: str


class SecureAskResponse(AskResponse):
    citations: list[SecureAskCitation]


class OriginalAskCitation(AskCitation):
    source_sha256: str
    original_url: str


class OriginalAskResponse(AskResponse):
    citations: list[OriginalAskCitation]


class AIClarifyOption(BaseModel):
    record_id: str
    label: str


class AIDemoChatRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    room_id: str
    model_id: str = Field(min_length=1, max_length=128)
    persona_id: str
    query: str = Field(min_length=1, max_length=4096)
    # Compatibility input: validate room ownership, never narrow safety evidence.
    selected_record_id: str | None = None

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        return AskRequest.nonblank_query(value)


class AIDemoChatResponse(BaseModel):
    """공개 채팅 응답 — 진단성 내부 데이터(원시 모델 출력·프롬프트·페르소나 원문)는
    Phase 0에서 제거됨. 필요 시 개발 전용 라우트로만 노출할 것 (NEXT_AGENT_HANDOFF P1)."""
    answer: str
    answer_source: str
    # 규칙 게이트 메타 (rule_gate 경로: REFUSE/CLARIFY/CONFLICT/NO_RECORD, 게이트 조치)
    gate_route: str | None = None
    gate_detail: str | None = None
    gate_action: str | None = None
    inference_source: str
    provider_name: str
    provider_mode: str
    selected_model: dict
    retrieved_evidence: list[str]
    citations: list[OriginalAskCitation] = Field(default_factory=list)
    # CLARIFY 경로의 선택형 후보 기록 제목 (UX-G02) — 무상태, 방 내부 기록만
    clarify_candidates: list[str] = []
    # 후보의 안정 식별자 계약 (감사 D) — record_id/label. 무파괴 추가 필드.
    clarify_options: list[AIClarifyOption] = []


class AIModelOption(BaseModel):
    id: str
    display_name: str
    provider: str
    placement: str
    supports_image: bool
    supports_audio: bool
    max_context_tokens: int
    notes: str


class AIPersonaOption(BaseModel):
    id: str
    label: str
    tone: str


class AIDemoBootstrapResponse(BaseModel):
    room_id: str
    room_name: str
    seeded_uploads: int
    seeded_files: int


class AIRuntimeStatusResponse(BaseModel):
    configured_models: list[str]
    gemma_endpoint_configured: bool
    exaone_endpoint_configured: bool
    prefer_ollama_for_gemma: bool
    prefer_ollama_for_exaone: bool
    ollama_base_url: str
    cloud_sql_connector_enabled: bool
    gcs_media_storage_enabled: bool
    gcp_credentials_path_configured: bool
    provider_timeout_seconds: int
    persona_count: int
