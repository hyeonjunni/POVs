"""Real adapter wiring through SECURE APIs. Default transport is a deterministic stub.

Explicit M41_PHYSICAL_FIXTURES switches to actual local VLM/STT/OCR/BGE calls;
the fixture directory must contain disposable, noncanonical verification media.
"""
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from app.processing.contracts import OCRResult
from app.retrieval.chunking import canonical_json, sha256_text
from sqlalchemy import text
from test_m4_multimodal import png, wav
from test_m4_secure import review_headers
from test_secure_catalogue import catalogue as _catalogue
from test_secure_identity import identity_client as _identity_client
from test_secure_media import content_path, record, upload
from test_secure_media import media_client as _media_client

catalogue = _catalogue
identity_client = _identity_client
media_client = _media_client


@pytest.mark.parametrize("asset", ["image-a.png", "image-b.png", "audio-a.wav"])
def test_adapters_private_review_correction_rejection(media_client, monkeypatch, tmp_path, asset):
    from app.core.config import settings
    from app.processing import multimodal_providers as providers
    from app.processing.multimodal_local import LocalImageOCR
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    from app.services.secure_local_providers import LocalEmbedding
    c = media_client
    physical = os.environ.get("M41_PHYSICAL_FIXTURES")
    tmp_path.chmod(0o700)
    for key, value in {"secure_provider_profile": "trusted-dgx-local", "secure_provider_temp_directory": str(tmp_path),
        "media_visual_provider": "ollama", "media_speech_provider": "transformers_whisper",
        "media_speech_python": os.environ.get("M41_SPEECH_PYTHON", sys.executable),
        "media_speech_model_path": os.environ.get("M41_SPEECH_MODEL", str(tmp_path)),
        "media_speech_device": "cuda" if physical else "cpu", "media_speech_language": "en"}.items():
        monkeypatch.setattr(settings, key, value)
    runtime = c.app.state.secure_runtime
    runtime.local_provider_admission = True
    runtime.embedding_provider = LocalEmbedding() if physical else SyntheticTokenHashProvider()
    calls = []
    original = providers._worker
    def worker(python, kind, payload, root, timeout):
        calls.append(kind)
        if physical:
            return original(python, kind, payload, root, timeout)
        if kind == "visual":
            return {"model": settings.media_visual_model, "done": True,
                "message": {"content": json.dumps({"visual_description": "A cup on a table", "objects": ["cup"], "people_count": 0})}}
        return {"text": "A picnic in the park", "language": "en",
                "segments": [{"text": "A picnic in the park", "start": 0.1, "end": 0.9}]}
    monkeypatch.setattr(providers, "_worker", worker)
    if not physical:
        monkeypatch.setattr(LocalImageOCR, "extract", lambda *a: OCRResult("SUMMER PICNIC 2009" if asset == "image-a.png" else "", 1, "eng"))
    audio = asset.endswith("wav")
    content = (Path(physical)/asset).read_bytes() if physical else (wav() if audio else png())
    source = upload(c, record(c, "voice" if audio else "image"), content, "audio/wav" if audio else "image/png")
    assert c.request("GET", content_path(source), headers=c.auth).content == content
    with c.engine.begin() as db:
        db.execute(text("UPDATE records SET visibility='PRIVATE' WHERE id=:r"), {"r": source["upload_id"]})
    created = c.request("POST", f'/records/{source["upload_id"]}/processing-jobs', headers=c.auth,
        json={"processor": "stt" if audio else "ocr", "upload_file_id": source["file_id"]})
    assert created.status_code == 200, created.text
    jid = created.json()["job_id"]

    # Owner has not run processing yet. Denials must precede materialization.
    viewers = []
    for role in ("member", "admin", "outsider"):
        second, _, _ = c.signup(username="m41-"+role, email="m41-"+role+"@example.com")
        auth = {"Authorization": "Bearer " + second["access_token"]}
        if role != "outsider":
            joined = c.request("POST", "/families/join", headers=auth, json={"invite_code": c.room["invite_code"],
                "related_to_user_id": c.session["user"]["user_id"], "relation_to_related_user": "child"})
            assert joined.status_code == 200, joined.text
        if role == "admin":
            with c.engine.begin() as db:
                db.execute(text("UPDATE family_members SET role='admin' WHERE user_id=:u"), {"u": second["user"]["user_id"]})
        viewers.append(auth)
    def denied():
        from app.api.routes import secure_media
        with monkeypatch.context() as check:
            check.setattr(secure_media, "open_bytes", lambda *a: pytest.fail("unauthorized decryption"))
            before = len(calls)
            for auth in viewers:
                for suffix, method, body in [("", "GET", None), ("/multimodal/run", "POST", None),
                    ("/multimodal/review", "POST", {"action": "approve", "source_attempt": 0,
                        "review_version": 0, "source_result_sha256": "0"*64})]:
                    options = {"json": body} if body else {}
                    headers = auth | {"Idempotency-Key": str(uuid4())}
                    actual = c.request(method, f"/processing-jobs/{jid}{suffix}", headers=headers, **options)
                    absent = c.request(method, f"/processing-jobs/{uuid4()}{suffix}", headers=headers, **options)
                    assert actual.status_code == absent.status_code == 404
                    assert actual.json() == absent.json()
            assert len(calls) == before
    denied()
    done = c.request("POST", f"/processing-jobs/{jid}/multimodal/run",
        headers=c.auth | {"Idempotency-Key": str(uuid4())})
    assert done.status_code == 200, done.text
    data = done.json()
    assert data["status"] == "SUCCEEDED", data
    result = data["result"]
    m = result["m4"]
    assert m["coverage"] == "COMPLETE" and m["review_state"] == "PENDING_REVIEW"
    assert m["approved"] is None and m["trusted_metadata"] == {} and result["text"] == ""
    if audio:
        predicted = m["predicted"]
        assert predicted["transcript"].strip() and predicted["language"] == "en"
        assert predicted["segments"]
        end = 0
        for segment in predicted["segments"]:
            assert 0 <= end <= segment["start_ms"] < segment["end_ms"] <= m["duration_ms"]
            end = segment["end_ms"]
    else:
        assert m["predicted"]["visual"]["visual_description"].strip()
        assert m["media_metadata"]["ocr_outcome"] == ("OCR_TEXT_FOUND" if asset == "image-a.png" else "OCR_EMPTY_SUCCESS")
    denied()
    def index():
        return c.request("POST", f'/records/{source["upload_id"]}/retrieval-index', headers=c.auth,
            json={"processing_job_id": jid})
    def search(query, auth=None):
        response = c.request("POST", f'/families/{c.room["room_id"]}/retrieval/search', headers=auth or c.auth,
            json={"query": query, "arm": "bm25", "k": 5})
        assert response.status_code == 200, response.text
        return response.json()["hits"]
    query = m["predicted"]["transcript"] if audio else m["predicted"]["visual"]["visual_description"]
    assert index().status_code != 200 and not search(query)
    def review(current, action, version, **extra):
        response = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=review_headers(c, source),
            json={"action": action, "source_attempt": data["attempt"], "review_version": version,
                "source_result_sha256": sha256_text(canonical_json(current)), **extra})
        assert response.status_code == 200, response.text
        return response.json()["result"]
    approved = review(result, "approve", 0)
    assert index().status_code == 200 and search(query)
    for auth in viewers[:2]:
        assert not search(query, auth)
    corrected = json.loads(json.dumps(approved["m4"]["approved"]))
    if audio:
        corrected["segments"][0]["text"] = "Corrected picnic recollection"
        corrected["transcript"] = " ".join(s["text"] for s in corrected["segments"])
    else:
        corrected["visual"]["visual_description"] = "Corrected still life observation"
    def temporal(year):
        return {"precision": "YEAR", "value": str(year), "granularity": "YEAR"}
    metadata = {"recorded_at": temporal(2026), "event_occurred_at": temporal(2009)}
    correction = review(approved, "correct", 1, approved=corrected, metadata=metadata)
    assert correction["m4"]["predicted"] == m["predicted"]
    assert correction["m4"]["approved"] == corrected
    assert correction["m4"]["trusted_metadata"]["recorded_at"] != correction["m4"]["trusted_metadata"]["event_occurred_at"]
    assert index().status_code == 200 and search("Corrected")
    rejected = review(correction, "reject", 2)
    assert rejected["text"] == "" and index().status_code != 200 and not search("Corrected")
    if physical:
        # Only disposable verification annotations; no production logging path.
        evidence = Path(os.environ["M41_EVIDENCE"])
        (evidence/(asset+'.json')).write_text(json.dumps({"noncanonical": True,
            "result": result, "provider_calls": calls, "review_index_rejection": "PASS",
            "private_pre_decryption": "PASS", "recorded_vs_event": "PASS"}, indent=2))
