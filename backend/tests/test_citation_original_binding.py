"""B3 exact original and replacement races; synthetic local bytes only."""
from ocr_approval_helpers import approve_job_fixture
from ocr_approval_helpers import confirmed_review

import hashlib
import json
from datetime import datetime
import pytest
from sqlalchemy import select, update
from app.api import deps
from app.api.routes import uploads
from app.main import app
from app.models.family import FamilyMember
from app.models.processing_job import ProcessingJob
from app.models.retrieval import RecordChunk, RecordEmbedding
from app.models.storage_allocation import LegacyStorageAllocation
from app.models.upload import UploadFile
from app.models.user import User
from app.services import media_loader
from app.services.retrieval_indexing import RetrievalIndexNotFound, RetrievalIndexNotReady, RetrievalIndexStale
from test_grounded_answering_api import rig, set_result, index, ask
from test_retrieval_indexing import FILE_ID, JOB_ID, USER_ID, RECORD_ID, ROOM_ID, OTHER_USER_ID

ORIGINAL = uploads.ONE_PIXEL_PNG + b"original A"
DIGEST = hashlib.sha256(ORIGINAL).hexdigest()
URL = f"/api/v1/uploads/{RECORD_ID}/files/{FILE_ID}/content?expected_sha256={DIGEST}"

@pytest.fixture
def original_case(rig, tmp_path, monkeypatch):
    tracker, embedding, provider, client = rig
    monkeypatch.setattr(uploads, "SessionLocal", tracker)
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(uploads, "get_gcs_client", lambda: None)
    def db_override():
        with tracker.session_maker() as db:
            yield db
    app.dependency_overrides[deps.get_db] = db_override
    app.dependency_overrides[deps.get_current_user] = lambda: User(id=USER_ID)
    set_result(tracker)
    with tracker.session_maker() as db:
        file = db.get(UploadFile, FILE_ID)
        file.storage_bucket = "local-media"
        file.storage_path = RECORD_ID + ".png"
        file.file_size = len(ORIGINAL)
        file.created_at = datetime(2000, 1, 1)
        allocation = LegacyStorageAllocation(
            id="citation-original-allocation",
            room_id=ROOM_ID,
            upload_id=RECORD_ID,
            storage_bucket="local-media",
            storage_path=file.storage_path,
            byte_size=len(ORIGINAL),
            state="ACTIVE",
            expires_at=datetime(2100, 1, 1),
        )
        db.add(allocation)
        file.storage_allocation_id = allocation.id
        job = db.get(ProcessingJob, JOB_ID)
        result = json.loads(job.result_json)
        result["source_sha256"] = DIGEST
        job.result_json = json.dumps(result)
        approve_job_fixture(job)
        db.commit()
    (tmp_path / (RECORD_ID + ".png")).write_bytes(ORIGINAL)
    index(tracker, embedding)
    return tracker, embedding, provider, client, tmp_path

def replace(client):
    result = client.post(f"/api/v1/uploads/{RECORD_ID}/binary",
        files={"file": ("replacement.png", uploads.ONE_PIXEL_PNG + b"original B", "image/png")})
    assert result.status_code == 200, result.text
    return result.json()["file_id"]

def test_citation_links_exact_verified_original(original_case):
    tracker, _, _, client, _ = original_case
    response = ask(client)
    assert response.status_code == 200, response.text
    citation = response.json()["citations"][0]
    assert citation["file_id"] == FILE_ID
    assert citation["source_sha256"] == DIGEST
    assert citation["original_url"] == URL
    opened = client.get(citation["original_url"])
    assert opened.status_code == 200, opened.text
    assert opened.content == ORIGINAL
    assert hashlib.sha256(opened.content).hexdigest() == citation["source_sha256"]
    assert tracker.open_count == 0

def test_normal_replacement_invalidates_index_and_old_citation(original_case):
    tracker, embedding, _, client, _ = original_case
    replace(client)
    with tracker.session_maker() as db:
        assert not db.scalars(select(RecordChunk).where(RecordChunk.record_id == RECORD_ID)).all()
        assert not db.scalars(select(RecordEmbedding)).all()
    response = ask(client)
    assert response.status_code == 200 and not response.json()["citations"], response.text
    assert client.get(URL).status_code in (404, 409)
    with pytest.raises((RetrievalIndexNotFound, RetrievalIndexNotReady, RetrievalIndexStale)):
        index(tracker, embedding)

def test_replacement_during_inference_rejects_old_answer(original_case):
    _, _, provider, client, _ = original_case
    provider.mutation = lambda _: replace(client)
    assert ask(client).status_code == 409

@pytest.mark.parametrize("mutation", ["replace", "revoke", "digest"])
def test_original_revalidates_after_read(original_case, monkeypatch, mutation):
    tracker, _, _, client, _ = original_case
    real_load = media_loader.load_media_bytes
    def changed(*args, **kwargs):
        assert tracker.open_count == 0
        content = real_load(*args, **kwargs)
        if mutation == "replace":
            replace(client)
        else:
            with tracker.session_maker() as db:
                if mutation == "revoke":
                    db.execute(update(FamilyMember).where(FamilyMember.user_id == USER_ID, FamilyMember.room_id == ROOM_ID).values(is_active=False))
                else:
                    job = db.get(ProcessingJob, JOB_ID)
                    result = json.loads(job.result_json)
                    result["source_sha256"] = "a" * 64
                    job.result_json = json.dumps(result)
                db.commit()
        return content
    monkeypatch.setattr(media_loader, "load_media_bytes", changed)
    assert client.get(URL).status_code in (403, 404, 409)

def test_changed_original_bytes_are_rejected(original_case):
    _, _, _, client, root = original_case
    (root / (RECORD_ID + ".png")).write_bytes(b"mutated")
    assert client.get(URL).status_code in (404, 409)

def test_original_foreign_and_no_jwt_are_rejected(original_case):
    _, _, _, client, _ = original_case
    app.dependency_overrides[deps.get_current_user] = lambda: User(id=OTHER_USER_ID)
    assert client.get(URL).status_code in (403, 404)
    app.dependency_overrides.pop(deps.get_current_user)
    app.dependency_overrides.pop(deps.get_authenticated_principal)
    assert client.get(URL).status_code == 401

def test_missing_digest_never_returns_original(original_case):
    tracker, _, _, client, _ = original_case
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob, JOB_ID)
        value = json.loads(job.result_json)
        del value["source_sha256"]
        job.result_json = json.dumps(value)
        approve_job_fixture(job)
        db.commit()
    assert client.get(URL).status_code in (404, 409)

def test_legacy_result_without_input_digest_cannot_answer(original_case):
    tracker, embedding, _, client, _ = original_case
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob, JOB_ID)
        value = json.loads(job.result_json)
        del value["source_sha256"]
        job.result_json = json.dumps(value)
        approve_job_fixture(job)
        db.commit()
    index(tracker, embedding)
    assert ask(client).status_code == 409


def test_replacement_while_embedding_rejects_stale_index_publication(original_case):
    tracker, embedding, _, client, _ = original_case
    original_embed = embedding.embed
    def embed(texts):
        vectors = original_embed(texts)
        replace(client)
        return vectors
    embedding.embed = embed
    with pytest.raises(RetrievalIndexStale):
        index(tracker, embedding)
    with tracker.session_maker() as db:
        assert not db.scalars(select(RecordChunk).where(RecordChunk.record_id == RECORD_ID)).all()
    assert client.get(URL).status_code == 409


def test_response_keeps_verified_bytes_without_lazy_reopen(original_case, monkeypatch):
    _, _, _, client, root = original_case
    real_snapshot = uploads._original_snapshot
    reads = 0
    def final_snapshot(*args):
        nonlocal reads
        snapshot = real_snapshot(*args)
        reads += 1
        if reads == 2:
            (root / (RECORD_ID + ".png")).write_bytes(b"later mutation")
        return snapshot
    monkeypatch.setattr(uploads, "_original_snapshot", final_snapshot)
    response = client.get(URL)
    assert response.status_code == 200
    assert response.content == ORIGINAL


def test_processing_persists_sha_of_exact_provider_input():
    from fastapi.testclient import TestClient
    from test_processing_api import _setup, _teardown, _run_with_media, processing_routes, RecordingOCRProvider
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        provider = RecordingOCRProvider(tracker)
        captured = []
        extract = provider.extract
        def capture(content, mime_type):
            captured.append(content)
            return extract(content, mime_type)
        provider.extract = capture
        processing_routes.PROVIDER_REGISTRY["ocr"] = provider
        response = _run_with_media(TestClient(app), tracker, ORIGINAL)
        assert response.status_code == 200 and response.json()["status"] == "SUCCEEDED"
        assert captured == [ORIGINAL]
        assert response.json()["result"]["source_sha256"] == hashlib.sha256(captured[0]).hexdigest()
    finally:
        _teardown(engine, old_sl, old_ocr)

@pytest.mark.parametrize("winner", ["index", "replacement"])
def test_concurrent_index_and_replacement_serialize_on_record(tmp_path, monkeypatch, winner):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from test_retrieval_indexing import setup_file_indexing, FakeEmbeddingProvider
    from app.services import retrieval_indexing
    engine, tracker = setup_file_indexing(tmp_path)
    with tracker.session_maker() as db:
        source_file = db.get(UploadFile, FILE_ID)
        source_file.file_size = 1
        allocation = LegacyStorageAllocation(
            id="citation-race-allocation",
            room_id=ROOM_ID,
            upload_id=RECORD_ID,
            storage_bucket="local-media",
            storage_path=source_file.storage_path,
            byte_size=1,
            state="ACTIVE",
            expires_at=datetime(2100, 1, 1),
        )
        db.add(allocation)
        source_file.storage_allocation_id = allocation.id
        db.commit()
    embedding = FakeEmbeddingProvider(tracker)
    monkeypatch.setattr(uploads, "SessionLocal", tracker)
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(uploads, "get_gcs_client", lambda: None)
    def get_db():
        with tracker.session_maker() as db:
            yield db
    app.dependency_overrides[deps.get_db] = get_db
    app.dependency_overrides[deps.get_current_user] = lambda: User(id=USER_ID)
    # Both operations reach publication after the index has copied source A.
    barrier = threading.Barrier(2)
    held, contender = threading.Event(), threading.Event()
    real_serialize = retrieval_indexing.serialize_record
    def serialize(db, record_id):
        role = db.info["b3_role"]
        if role == winner:
            real_serialize(db, record_id)
            held.set()
            assert contender.wait(timeout=10)
        else:
            contender.set()
            real_serialize(db, record_id)
    monkeypatch.setattr(retrieval_indexing, "serialize_record", serialize)
    # Replacement now correctly locks the room before the record; observe
    # contention before that first lock instead of waiting behind it.
    from app.services import storage_quota
    real_room_lock = storage_quota._lock_room
    def room_lock(db, room_id):
        if winner == "index" and held.is_set():
            contender.set()
        return real_room_lock(db, room_id)
    monkeypatch.setattr(storage_quota, "_lock_room", room_lock)
    # TestClient executes the route on a worker, so label sessions at their factories.
    def replacement_session():
        session = tracker()
        session.info["b3_role"] = "replacement"
        return session
    def index_session():
        session = tracker()
        session.info["b3_role"] = "index"
        return session
    monkeypatch.setattr(uploads, "SessionLocal", replacement_session)
    def run_index():
        try:
            retrieval_indexing.index_processing_result(
                record_id=RECORD_ID, processing_job_id=JOB_ID, user_id=USER_ID,
                provider=embedding, session_factory=index_session)
            return "published"
        except RetrievalIndexStale:
            return "stale"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            if winner == "index":
                indexed = pool.submit(run_index)
                assert held.wait(timeout=10)
                uploaded = pool.submit(replace, TestClient(app))
            else:
                uploaded = pool.submit(replace, TestClient(app))
                assert held.wait(timeout=10)
                indexed = pool.submit(run_index)
            new_file = uploaded.result(timeout=20)
            assert indexed.result(timeout=20) == ("published" if winner == "index" else "stale")
        assert held.is_set() and contender.is_set()
        with tracker.session_maker() as db:
            assert uploads.get_latest_upload_file(db, RECORD_ID).id == new_file
            assert not db.scalars(select(RecordChunk).where(RecordChunk.record_id == RECORD_ID)).all()
            assert not db.scalars(select(RecordEmbedding)).all()
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

@pytest.mark.parametrize("missing", ["bytes", "locator"])
def test_missing_original_fails_closed(original_case, missing):
    tracker, _, _, client, root = original_case
    if missing == "bytes":
        (root / (RECORD_ID + ".png")).unlink()
    else:
        with tracker.session_maker() as db:
            db.get(UploadFile, FILE_ID).storage_path = None
            db.commit()
    assert client.get(URL).status_code in (404, 409)

def test_distinct_job_digests_on_same_file_stay_bound_to_their_citations(original_case):
    tracker, embedding, _, client, _ = original_case
    second_digest = hashlib.sha256(b"another exact provider input").hexdigest()
    with tracker.session_maker() as db:
        source = json.loads(db.get(ProcessingJob, JOB_ID).result_json)
        source.update(extractor="AUTO_STT", extractor_id="synthetic-stt", source_sha256=second_digest)
        db.add(ProcessingJob(id="b3-second-job", record_id=RECORD_ID, upload_file_id=FILE_ID,
            room_id=ROOM_ID, processor="stt", status="SUCCEEDED", attempt=0, result_json=json.dumps(source),
            review_json=confirmed_review(source, 0)))
        db.commit()
    index(tracker, embedding, job_id="b3-second-job")
    response = ask(client, k=2)
    assert response.status_code == 200, response.text
    citations = response.json()["citations"]
    assert len(citations) == 2 and {c["file_id"] for c in citations} == {FILE_ID}
    assert {c["extractor"]: c["source_sha256"] for c in citations} == {
        "AUTO_OCR": DIGEST, "AUTO_STT": second_digest}


@pytest.mark.parametrize("after_read", [False, True])
def test_rejected_ocr_cannot_publish_original(original_case, monkeypatch, after_read):
    tracker, _, _, client, _ = original_case
    from app.retrieval.chunking import canonical_json, sha256_text
    def reject():
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, JOB_ID)
            job.review_json = json.dumps({
                "status": "rejected", "source_attempt": job.attempt,
                "source_result_sha256": sha256_text(canonical_json(json.loads(job.result_json))),
            })
            db.commit()
    if after_read:
        real_load = media_loader.load_media_bytes
        def load(*args, **kwargs):
            content = real_load(*args, **kwargs)
            reject()
            return content
        monkeypatch.setattr(media_loader, "load_media_bytes", load)
    else:
        reject()
    response = client.get(URL)
    assert response.status_code == 409
    assert ORIGINAL not in response.content
