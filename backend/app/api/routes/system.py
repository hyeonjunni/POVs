from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.core.database import SessionLocal


router = APIRouter()


@router.get("/health/db")
def database_healthcheck():
    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
        return {
            "database": "connected",
        }
    finally:
        session.close()


@router.get("/health/ollama")
def ollama_healthcheck():
    return {"ollama": "diagnostics-disabled"}


@router.get("/health/gcp")
def gcp_healthcheck():
    return {
        "gcs_media_storage_enabled": False,
        "legacy_media_storage": "local-only",
    }


@router.post("/health/gcp/storage-smoke-test")
def gcp_storage_smoke_test():
    raise HTTPException(
        status_code=409,
        detail="LEGACY plaintext GCS uploads are disabled; media storage is local only",
    )
