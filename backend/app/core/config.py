import re
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    security_mode: str = "LEGACY"
    secure_generation_id: str = ""
    secure_unit_name: str = ""
    secure_approved_uid: int = 0
    secure_credentials_directory: str = ""
    secure_ciphertext_directory: str = ""
    secure_provider_profile: str = "synthetic"
    secure_provider_temp_directory: str = ""
    # Opt-in owned provider unit (candidate only): its cgroup is killed and proven empty on uncertain completion.
    secure_owned_provider_port: int = 0
    secure_owned_provider_cgroup: str = ""
    secure_ciphertext_adapter: str = "local"
    secure_gcs_bucket: str = ""
    secure_gcs_project_id: str = ""
    secure_lock_path: str = ""
    secure_worker_lock_path: str = ""
    app_name: str = "Ambient Legacy Backend"
    record_audit_retention_days: int = Field(default=180, ge=180)
    quarantined_record_ids: list[UUID] = Field(default_factory=list)
    media_image_max_bytes: int = Field(default=20_000_000, gt=0)
    media_audio_max_bytes: int = Field(default=100_000_000, gt=0)
    media_video_max_bytes: int = Field(default=500_000_000, gt=0)
    media_audio_max_duration_ms: int = Field(default=900_000, gt=0)
    media_video_max_duration_ms: int = Field(default=600_000, gt=0)
    # Opt-in M4.1 providers; operator settings, never request-controlled.
    media_visual_provider: str = "none"
    media_visual_model: str = "gemma3:27b"
    media_visual_endpoint: str = "http://127.0.0.1:11434"
    media_visual_timeout_seconds: int = Field(default=180, ge=1, le=600)
    media_speech_provider: str = "none"
    media_speech_python: str = ""
    media_speech_model_path: str = ""
    media_speech_device: str = "cpu"
    media_speech_language: str = "ko"
    media_speech_timeout_seconds: int = Field(default=180, ge=1, le=600)
    api_prefix: str = "/api/v1"
    database_url: str = "postgresql+psycopg://user:password@localhost:5432/ambient_legacy"
    gcs_bucket_name: str = "ambient-legacy"
    gcp_project_id: str = ""
    gcp_credentials_path: str = ""
    # Deprecated for LEGACY; retained only as an ignored compatibility setting.
    use_gcs_media_storage: bool = True
    legacy_room_storage_quota_bytes: int = 1024 * 1024 * 1024
    use_cloud_sql_connector: bool = False
    instance_connection_name: str = ""
    db_user: str = ""
    db_pass: str = ""
    db_name: str = ""
    private_ip: bool = False
    gemma_endpoint_url: str = ""
    exaone_endpoint_url: str = ""
    ollama_base_url: str = "http://127.0.0.1:11434"
    prefer_ollama_for_gemma: bool = True
    prefer_ollama_for_exaone: bool = False
    ollama_gemma_e2b_model_name: str = "gemma4:e2b"
    ollama_gemma_e4b_model_name: str = "gemma4:e4b"
    ollama_gemma_27b_model_name: str = "gemma3:27b"
    ollama_exaone_model_name: str = "exaone3.5:32b"
    ollama_exaone_deep_model_name: str = "exaone-deep:32b"
    ollama_exaone_40_model_name: str = "hf.co/LGAI-EXAONE/EXAONE-4.0-32B-GGUF:Q4_K_M"
    ollama_exaone_45_model_name: str = "hf.co/LGAI-EXAONE/EXAONE-4.5-33B-GGUF:Q4_K_M"
    ollama_qwen_32b_tuned_model_name: str = "ambient-legacy-qwen25-32b-v4:latest"
    ollama_qwen_72b_model_name: str = "qwen2.5:72b"
    ai_provider_timeout_seconds: int = 30
    jwt_secret_key: str = ""
    jwt_access_token_expires_minutes: int = 60 * 24 * 7
    # Local command-backed OCR/STT (Milestone 1 Task 5) — server-configured
    # executable names only; never accept an executable path from a client.
    ocr_executable_path: str = ""
    ocr_timeout_seconds: int = 30
    ocr_language: str = "kor+eng"
    ocr_psm: int = Field(default=6, ge=0, le=13)
    stt_executable_path: str = ""
    stt_timeout_seconds: int = 60
    stt_language: str = "ko"
    # Bounded processing I/O (FOUNDATION_AUDIT_1_AMENDMENT — AUDIT-1 verification gap).
    ocr_max_input_bytes: int = 20 * 1024 * 1024
    text_max_input_bytes: int = Field(default=5_000_000, ge=1)
    stt_max_input_bytes: int = 200 * 1024 * 1024
    processing_max_output_bytes: int = 8 * 1024 * 1024
    retrieval_embedding_endpoint_url: str = ""
    retrieval_embedding_model_key: str = ""
    retrieval_embedding_batch_size: int = 32
    retrieval_embedding_timeout_seconds: int = 30
    retrieval_candidate_limit: int = 200
    retrieval_vector_relevance_floor: float = Field(default=0.55, ge=0, le=1, allow_inf_nan=False)

    @field_validator(
        "use_gcs_media_storage",
        "use_cloud_sql_connector",
        "private_ip",
        "prefer_ollama_for_gemma",
        "prefer_ollama_for_exaone",
        mode="before",
    )
    @classmethod
    def normalize_bool_env(cls, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def validate_secure_config(config):
    """Reject conflicts before environment mutation, connector creation or SQL."""
    from sqlalchemy.engine import make_url
    from app.services.secure_runtime import SecureUnavailable
    secret = config.jwt_secret_key
    # Require an operator-supplied key, not the public fallback or trivial padding.
    if (not isinstance(secret, str) or len(secret.encode("utf-8")) < 32
            or len(set(secret)) < 8 or secret != secret.strip()
            or any(public in secret.lower() for public in ("dev-secret", "development-secret", "default-secret", "changeme", "change-me"))):
        raise SecureUnavailable()
    if config.security_mode == "LEGACY":
        return
    try:
        if config.secure_provider_profile not in ("synthetic", "trusted-dgx-local"):
            raise SecureUnavailable()
        if config.secure_provider_profile == "synthetic" and config.secure_provider_temp_directory:
            raise SecureUnavailable()
        if config.secure_provider_profile == "trusted-dgx-local":
            from app.services.secure_local_providers import _private_root
            _private_root(config.secure_provider_temp_directory)
        if config.secure_ciphertext_adapter == "local":
            if config.secure_gcs_bucket or config.secure_gcs_project_id:
                raise SecureUnavailable()
        elif config.secure_ciphertext_adapter == "gcs":
            if (re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", config.secure_gcs_bucket) is None
                    or re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", config.secure_gcs_project_id) is None):
                raise SecureUnavailable()
        else:
            raise SecureUnavailable()
        url = make_url(config.database_url)
        if (config.security_mode != "SECURE" or url.drivername not in ("sqlite", "sqlite+pysqlite")
                or url.host or url.username or url.password or url.query or not url.database
                or url.database.startswith(("//", "\\\\"))
                or config.use_cloud_sql_connector or config.instance_connection_name
                or config.gcp_credentials_path or config.gcp_project_id or config.use_gcs_media_storage
                or config.gemma_endpoint_url or config.exaone_endpoint_url
                or config.prefer_ollama_for_gemma or config.prefer_ollama_for_exaone
                or config.retrieval_embedding_endpoint_url or config.ocr_executable_path or config.stt_executable_path
):
            raise SecureUnavailable()
    except Exception:
        raise SecureUnavailable() from None


settings = Settings()
