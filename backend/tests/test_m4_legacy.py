"""LEGACY M4 review persistence and approval gates on disposable SQLite."""
from uuid import uuid4

import pytest
from app.processing.multimodal import (
    SyntheticImageOCR,
    SyntheticVisualProvider,
    extract,
)
from app.services.multimodal_jobs import legacy_review
from app.services.processing_jobs import (
    create_job,
    mark_running,
    mark_succeeded,
    processing_result_is_evidence,
)
from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request
from test_m4_multimodal import png, request
from test_processing_jobs import RECORD_ID, UPLOAD_FILE_ID, USER_ID
from test_processing_jobs import session as _session

session = _session


def test_legacy_approval_correction_replay_and_rejection(session, monkeypatch):
    from app.api.routes import secure_operations
    monkeypatch.setattr(secure_operations, "principal", lambda request: USER_ID)
    job = create_job(session, record_id=RECORD_ID, room_id="room-pj-1", upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    mark_running(session, job.id)
    result = extract(png(), "image/png", ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider())
    mark_succeeded(session, job.id, result=result, attempt=0)
    def http(version, key):
        return Request({"type": "http", "method": "POST", "path": f"/processing-jobs/{job.id}/multimodal/review",
            "headers": [(b"if-match", f'"{version}"'.encode()), (b"idempotency-key", key.encode())],
            "scheme": "http", "server": ("test", 80), "query_string": b""})
    key = str(uuid4())
    payload = request(result)
    approved = legacy_review(session, USER_ID, job.id, payload, http(1, key))
    assert processing_result_is_evidence(session.get(type(job), job.id))
    replay = legacy_review(session, USER_ID, job.id, payload, http(1, key))
    assert replay == approved
    rejected = legacy_review(session, USER_ID, job.id, request(approved["result"], "reject"), http(2, str(uuid4())))
    assert rejected["result"]["text"] == ""
    assert not processing_result_is_evidence(session.get(type(job), job.id))
    assert session.scalar(text("SELECT count(*) FROM record_audits WHERE action LIKE 'M4_REVIEW_%'")) == 2
    with pytest.raises(HTTPException) as conflict:
        legacy_review(session, USER_ID, job.id, payload, http(1, str(uuid4())))
    assert conflict.value.status_code == 412
