"""M4 through the SECURE boundary and ciphertext journal, synthetic providers."""

from uuid import uuid4

import pytest
from app.retrieval.chunking import canonical_json, sha256_text
from sqlalchemy import text
from test_secure_catalogue import catalogue as _catalogue
from test_secure_identity import identity_client as _identity_client
from test_secure_media import job, upload
from test_secure_media import media_client as _media_client

catalogue = _catalogue
identity_client = _identity_client
media_client = _media_client


def test_encrypted_image_review_and_index(media_client):
    c = media_client
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    source = upload(c)
    queued = job(c, source)
    jid = queued["job_id"]
    result = c.request("POST", f"/processing-jobs/{jid}/multimodal/run", headers=c.auth | {"Idempotency-Key": str(uuid4())})
    assert result.status_code == 200, result.text
    result = result.json()
    assert result["status"] == "SUCCEEDED", result
    assert result["result"]["m4"]["media_metadata"]["ocr_outcome"] == "OCR_EMPTY_SUCCESS"
    indexed = c.request("POST", f'/records/{source["upload_id"]}/retrieval-index', headers=c.auth,
        json={"processing_job_id": jid})
    assert indexed.status_code != 200
    reviewed = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=review_headers(c, source),
        json={"action": "approve", "source_attempt": result["attempt"], "review_version": 0,
              "source_result_sha256": sha256_text(canonical_json(result["result"]))})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["result"]["m4"]["review_state"] == "APPROVED"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT result_json FROM processing_jobs WHERE id=:j"), {"j": jid}) is None
        raw = str(db.execute(text("SELECT * FROM processing_jobs")).all())
        assert "Synthetic visual description" not in raw
    indexed = c.request("POST", f'/records/{source["upload_id"]}/retrieval-index', headers=c.auth,
        json={"processing_job_id": jid})
    assert indexed.status_code == 200, indexed.text
    current = reviewed.json()["result"]
    rejected = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=review_headers(c, source),
        json={"action": "reject", "source_attempt": 0, "review_version": 1,
              "source_result_sha256": sha256_text(canonical_json(current))})
    assert rejected.status_code == 200, rejected.text
    indexed = c.request("POST", f'/records/{source["upload_id"]}/retrieval-index', headers=c.auth,
        json={"processing_job_id": jid})
    assert indexed.status_code != 200


@pytest.mark.parametrize("modality", ["image", "voice", "video"])
@pytest.mark.parametrize("viewer", ["admin", "member", "outsider"])
def test_private_annotations_denied_before_decryption(media_client, monkeypatch, modality, viewer):
    from app.processing.multimodal import SyntheticVideoDecoder
    from test_m4_multimodal import wav
    from test_secure_media import PNG, record
    c = media_client
    content, mime = {"image": (PNG, "image/png"), "voice": (wav(), "audio/wav"),
                     "video": (SyntheticVideoDecoder.marker, "video/mp4")}[modality]
    source = upload(c, record(c, modality), content, mime)
    created = c.request("POST", f'/records/{source["upload_id"]}/processing-jobs', headers=c.auth,
        json={"processor": "ocr" if modality == "image" else "stt", "upload_file_id": source["file_id"]})
    assert created.status_code == 200, created.text
    queued = created.json()
    jid = queued["job_id"]
    done = c.request("POST", f"/processing-jobs/{jid}/multimodal/run", headers=c.auth | {"Idempotency-Key": str(uuid4())})
    assert done.status_code == 200
    second, _, _ = c.signup(username="m4-second", email="m4-second@example.com")
    auth = {"Authorization": "Bearer " + second["access_token"]}
    if viewer != "outsider":
        joined = c.request("POST", "/families/join", headers=auth, json={"invite_code": c.room["invite_code"],
            "related_to_user_id": c.session["user"]["user_id"], "relation_to_related_user": "child"})
        assert joined.status_code == 200
    with c.engine.begin() as db:
        db.execute(text("UPDATE records SET visibility='PRIVATE' WHERE id=:r"), {"r": source["upload_id"]})
        if viewer == "admin":
            db.execute(text("UPDATE family_members SET role='admin' WHERE user_id=:u"), {"u": second["user"]["user_id"]})
    from app.api.routes import secure_media
    reads = []
    original = secure_media.open_bytes
    def observed(*args):
        reads.append(True)
        return original(*args)
    monkeypatch.setattr(secure_media, "open_bytes", observed)
    missing = str(uuid4())
    for suffix, method, body in [("", "GET", None), ("/multimodal/run", "POST", None),
        ("/multimodal/review", "POST", {"action": "approve", "source_attempt": 0,
        "review_version": 0, "source_result_sha256": "0" * 64})]:
        options = {"json": body} if body else {}
        denied = c.request(method, f"/processing-jobs/{jid}{suffix}", headers=auth, **options)
        absent = c.request(method, f"/processing-jobs/{missing}{suffix}", headers=auth, **options)
        assert denied.status_code == absent.status_code == 404
        assert denied.json() == absent.json()
    assert not reads
    if viewer != "outsider":
        from app.retrieval.evaluation import SyntheticTokenHashProvider
        c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
        with c.engine.connect() as db:
            forbidden = set(db.execute(text("SELECT object_id FROM protected_pointers WHERE owner_type IN ('record','file','job','record_index') AND object_id IS NOT NULL")).scalars())
        store = c.app.state.secure_runtime.store
        original_read = store.read
        def filtered_read(receipt):
            assert receipt.object_name not in forbidden, "Unauthorized private object reached decryption"
            return original_read(receipt)
        monkeypatch.setattr(store, "read", filtered_read)
        found = c.request("POST", f'/families/{c.room["room_id"]}/retrieval/search', headers=auth,
            json={"query": "Synthetic", "arm": "bm25", "k": 5})
        assert found.status_code == 200, found.text
        assert found.json()["hits"] == []


def test_source_replacement_blocks_old_media_review(media_client):
    c = media_client
    source = upload(c)
    jid = job(c, source)["job_id"]
    done = c.request("POST", f"/processing-jobs/{jid}/multimodal/run", headers=c.auth | {"Idempotency-Key": str(uuid4())}).json()
    from test_secure_media import PNG
    upload(c, source, PNG + b"replacement")
    reviewed = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=review_headers(c, source),
        json={"action": "approve", "source_attempt": done["attempt"], "review_version": 0,
            "source_result_sha256": sha256_text(canonical_json(done["result"]))})
    assert reviewed.status_code == 409


def review_headers(c, source):
    record = c.request("GET", f'/records/{source["upload_id"]}', headers=c.auth)
    assert record.status_code == 200, record.text
    return c.auth | {"If-Match": record.headers["etag"], "Idempotency-Key": str(uuid4())}


def test_encrypted_audio_and_video_review_timeline(media_client):
    from app.processing.multimodal import SyntheticVideoDecoder
    from test_m4_multimodal import wav
    from test_secure_media import record
    c = media_client
    for kind, mime, content in [("voice", "audio/wav", wav()),
        ("video", "video/mp4", SyntheticVideoDecoder.marker)]:
        source = upload(c, record(c, kind), content, mime)
        created = c.request("POST", f'/records/{source["upload_id"]}/processing-jobs', headers=c.auth,
            json={"processor": "stt", "upload_file_id": source["file_id"]})
        assert created.status_code == 200, created.text
        jid = created.json()["job_id"]
        done = c.request("POST", f"/processing-jobs/{jid}/multimodal/run", headers=c.auth | {"Idempotency-Key": str(uuid4())})
        assert done.status_code == 200, done.text
        result = done.json()["result"]
        assert done.json()["status"] == "SUCCEEDED", done.json()
        assert result["m4"]["duration_ms"] == 1000
        if kind == "video":
            assert [f["timestamp_ms"] for f in result["m4"]["predicted"]["frames"]] == [0, 500]
        approved = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=review_headers(c, source),
            json={"action": "approve", "source_attempt": 0, "review_version": 0,
                "source_result_sha256": sha256_text(canonical_json(result))})
        assert approved.status_code == 200, approved.text
        assert approved.json()["result"]["m4"]["approved"]["transcript"] == "Synthetic speech"


def test_review_retry_and_stale_media_citation(media_client):
    from urllib.parse import urlsplit

    from app.retrieval.evaluation import SyntheticTokenHashProvider
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    source = upload(c)
    jid = job(c, source)["job_id"]
    run_headers = c.auth | {"Idempotency-Key": str(uuid4())}
    done = c.request("POST", f"/processing-jobs/{jid}/multimodal/run", headers=run_headers)
    replay = c.request("POST", f"/processing-jobs/{jid}/multimodal/run", headers=run_headers)
    assert done.status_code == replay.status_code == 200 and done.json() == replay.json()
    result = done.json()["result"]
    payload = {"action": "approve", "source_attempt": 0, "review_version": 0,
        "source_result_sha256": sha256_text(canonical_json(result))}
    headers = review_headers(c, source)
    approved = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=headers, json=payload)
    repeated = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=headers, json=payload)
    assert approved.status_code == repeated.status_code == 200 and approved.json() == repeated.json()
    indexed = c.request("POST", f'/records/{source["upload_id"]}/retrieval-index', headers=c.auth,
        json={"processing_job_id": jid})
    assert indexed.status_code == 200, indexed.text
    found = c.request("POST", f'/families/{c.room["room_id"]}/retrieval/search', headers=c.auth,
        json={"query": "Synthetic visual description", "arm": "bm25", "k": 5})
    assert found.status_code == 200, found.text
    hits = found.json()["hits"]
    assert hits and hits[0]["source_locator"]["kind"] == "multimodal"
    current = approved.json()["result"]
    correction = dict(current["m4"]["approved"])
    correction["visual"] = dict(correction["visual"], visual_description="Corrected visual description")
    corrected = c.request("POST", f"/processing-jobs/{jid}/multimodal/review", headers=review_headers(c, source),
        json={"action": "correct", "source_attempt": 0, "review_version": 1, "approved": correction,
            "source_result_sha256": sha256_text(canonical_json(current))})
    assert corrected.status_code == 200, corrected.text
    # Existing exact source endpoint binds the processing result hash as well as bytes.
    original = urlsplit(source["file_url"])
    suffix = original.query + f'&processing_job_id={jid}&source_attempt=0&source_result_sha256={sha256_text(canonical_json(current))}'
    stale = c.request("GET", original.path.removeprefix("/api/v1") + "?" + suffix, headers=c.auth)
    assert stale.status_code == 409, stale.text
