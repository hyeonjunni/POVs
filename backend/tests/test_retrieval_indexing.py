from __future__ import annotations

import hashlib
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine
from test_processing_api import SessionOpenTracker
from app.core.database import Base
from app.models.auth_identity import AuthIdentity, PrincipalNotPersisted
from app.models.family import FamilyMember, FamilyRoom
from app.models.processing_job import ProcessingJob
from app.models.retrieval import M2_EMBEDDING_DIMENSIONS, RecordChunk, RecordEmbedding
from app.models.upload import Upload, UploadFile
from app.models.user import User
from app.retrieval.embeddings import EmbeddingProviderError, HTTPEmbeddingProvider
from app.services.retrieval_indexing import (
    RetrievalIndexNotFound,
    RetrievalIndexNotReady,
    RetrievalIndexStale,
    index_processing_result,
)
from app.services.account_cleanup import delete_upload_record
from app.services.retrieval import search_room
from app.services.record_policy import register_legacy_record

ROOM_ID = "room-ri-1"
OTHER_ROOM_ID = "room-ri-2"
USER_ID = "user-ri-1"
OTHER_USER_ID = "user-ri-2"
RECORD_ID = "record-ri-1"
OTHER_RECORD_ID = "record-ri-2"
FILE_ID = "file-ri-1"
OTHER_FILE_ID = "file-ri-2"
JOB_ID = "job-ri-1"
OTHER_JOB_ID = "job-ri-2"
MODEL_KEY = "bge-m3@test-revision"


def result_payload(text: str = "할머니와 여름 바다를 걸었던 기억") -> dict:
    return {
        "text": text,
        "source_sha256": hashlib.sha256(b"synthetic retrieval source bytes").hexdigest(),
        "extractor": "AUTO_OCR",
        "extractor_id": "tesseract:test-revision",
        "review_status": "unreviewed",
        "user_confirmed": False,
    }


from ocr_approval_helpers import confirmed_review, approve_job_fixture


def _job(job_id: str, record_id: str, file_id: str, room_id: str) -> ProcessingJob:
    return ProcessingJob(
        id=job_id,
        record_id=record_id,
        upload_file_id=file_id,
        room_id=room_id,
        processor="ocr",
        status="SUCCEEDED",
        attempt=1,
        result_json=json.dumps(result_payload(), ensure_ascii=False),
        review_json=confirmed_review(result_payload()),
    )


def seed(session_maker) -> None:
    with session_maker() as db:
        db.add_all(
            [
                User(
                    id=USER_ID,
                    google_sub=None,
                    username="ri_user",
                    password_hash="x",
                    email="ri@example.test",
                    name="RI User",
                ),
                User(
                    id=OTHER_USER_ID,
                    google_sub=None,
                    username="ri_other",
                    password_hash="x",
                    email="ri-other@example.test",
                    name="RI Other",
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                AuthIdentity(
                    id="identity-ri-1",
                    user_id=USER_ID,
                    provider="local",
                    provider_subject="ri_user",
                    credential_hash="x",
                ),
                AuthIdentity(
                    id="identity-ri-2",
                    user_id=OTHER_USER_ID,
                    provider="local",
                    provider_subject="ri_other",
                    credential_hash="x",
                ),
                FamilyRoom(
                    id=ROOM_ID,
                    name="내 방",
                    invite_code="RI0001",
                    owner_user_id=USER_ID,
                ),
                FamilyRoom(
                    id=OTHER_ROOM_ID,
                    name="남의 방",
                    invite_code="RI0002",
                    owner_user_id=OTHER_USER_ID,
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                FamilyMember(id="member-ri-1", room_id=ROOM_ID, user_id=USER_ID, role="owner"),
                FamilyMember(
                    id="member-ri-2", room_id=OTHER_ROOM_ID, user_id=OTHER_USER_ID, role="owner"
                ),
                Upload(
                    id=RECORD_ID,
                    room_id=ROOM_ID,
                    uploader_user_id=USER_ID,
                    type="image",
                    title="우리 기록",
                    status="uploaded",
                ),
                Upload(
                    id=OTHER_RECORD_ID,
                    room_id=OTHER_ROOM_ID,
                    uploader_user_id=OTHER_USER_ID,
                    type="image",
                    title="남의 기록",
                    status="uploaded",
                ),
            ]
        )
        db.flush()
        register_legacy_record(db, RECORD_ID)
        register_legacy_record(db, OTHER_RECORD_ID)
        db.add_all(
            [
                UploadFile(
                    id=FILE_ID,
                    upload_id=RECORD_ID,
                    storage_bucket="local-media",
                    storage_path="ours.png",
                    file_uploader_user_id=USER_ID,
                    mime_type="image/png",
                ),
                UploadFile(
                    id=OTHER_FILE_ID,
                    upload_id=OTHER_RECORD_ID,
                    storage_bucket="local-media",
                    storage_path="theirs.png",
                    file_uploader_user_id=OTHER_USER_ID,
                    mime_type="image/png",
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                _job(JOB_ID, RECORD_ID, FILE_ID, ROOM_ID),
                _job(OTHER_JOB_ID, OTHER_RECORD_ID, OTHER_FILE_ID, OTHER_ROOM_ID),
            ]
        )
        db.commit()


def setup_indexing():
    engine = fk_enforced_engine("sqlite://")
    Base.metadata.create_all(engine)
    tracker = SessionOpenTracker(engine)
    seed(tracker.session_maker)
    return engine, tracker


def setup_file_indexing(tmp_path):
    engine = fk_enforced_engine(f"sqlite:///{tmp_path / 'retrieval-indexing.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    tracker = SessionOpenTracker(engine)
    seed(tracker.session_maker)
    return engine, tracker


class FakeEmbeddingProvider:
    model_key = MODEL_KEY
    dimensions = M2_EMBEDDING_DIMENSIONS

    def __init__(self, tracker: SessionOpenTracker, *, vectors=None, mutation=None, error=None):
        self.tracker = tracker
        self.vectors = vectors
        self.mutation = mutation
        self.error = error
        self.open_count_at_embed: list[int] = []

    def embed(self, texts):
        self.open_count_at_embed.append(self.tracker.open_count)
        if self.mutation:
            self.mutation(self.tracker.session_maker)
        if self.error:
            raise self.error
        if self.vectors is not None:
            return self.vectors
        return [[float(index + 1)] * self.dimensions for index, _ in enumerate(texts)]


def index(tracker, provider, *, record_id=RECORD_ID, job_id=JOB_ID, user_id=USER_ID):
    return index_processing_result(
        record_id=record_id,
        processing_job_id=job_id,
        user_id=user_id,
        provider=provider,
        session_factory=tracker,
    )


def persisted_rows(tracker):
    with tracker.session_maker() as db:
        chunks = db.scalars(select(RecordChunk).order_by(RecordChunk.id)).all()
        embeddings = db.scalars(select(RecordEmbedding).order_by(RecordEmbedding.id)).all()
        return (
            [(row.id, row.source_attempt, row.source_result_sha256, row.text) for row in chunks],
            [(row.id, row.chunk_id, row.model_key, row.embedding) for row in embeddings],
        )


def searchable_ids(tracker):
    return {
        arm: [hit.chunk.id for hit in search_room(
            room_id=ROOM_ID, user_id=USER_ID, query="바다를", arm=arm, k=10,
            provider=FakeEmbeddingProvider(tracker), session_factory=tracker,
            candidate_limit=200,
        )]
        for arm in ("substring", "bm25", "vector", "hybrid_rrf")
    }


def test_record_deletion_during_reindex_cannot_resurrect_retrieval_rows():
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        assert all(searchable_ids(tracker).values())

        def delete_record(session_maker):
            with session_maker() as db:
                delete_upload_record(db, db.get(Upload, RECORD_ID))
                db.commit()

        provider = FakeEmbeddingProvider(tracker, mutation=delete_record)
        with pytest.raises(RetrievalIndexStale):
            index(tracker, provider)
        assert persisted_rows(tracker) == ([], [])
        assert searchable_ids(tracker) == dict.fromkeys(
            ("substring", "bm25", "vector", "hybrid_rrf"), []
        )
        assert provider.open_count_at_embed == [0]
    finally:
        engine.dispose()


def test_persisted_principal_is_required_before_record_or_acl_lookup():
    engine, tracker = setup_indexing()
    try:
        provider = FakeEmbeddingProvider(tracker)
        with pytest.raises(PrincipalNotPersisted):
            index(tracker, provider, record_id="missing", job_id="missing", user_id="ghost")
        assert provider.open_count_at_embed == []
        assert persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("record_id", "job_id"),
    [
        ("missing", "missing"),
        (OTHER_RECORD_ID, OTHER_JOB_ID),
        ("missing", JOB_ID),
        (OTHER_RECORD_ID, JOB_ID),
    ],
)
def test_missing_and_foreign_sources_are_the_same_not_found(record_id, job_id):
    engine, tracker = setup_indexing()
    try:
        provider = FakeEmbeddingProvider(tracker)
        with pytest.raises(RetrievalIndexNotFound, match="Retrieval source not found"):
            index(tracker, provider, record_id=record_id, job_id=job_id)
        assert provider.open_count_at_embed == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda job: setattr(job, "record_id", OTHER_RECORD_ID),
        lambda job: setattr(job, "upload_file_id", OTHER_FILE_ID),
        lambda job: setattr(job, "room_id", OTHER_ROOM_ID),
    ],
    ids=["wrong-record", "wrong-file", "wrong-room"],
)
def test_wrong_record_file_or_room_job_linkage_is_rejected(mutate):
    engine, tracker = setup_indexing()
    try:
        with tracker.session_maker() as db:
            mutate(db.get(ProcessingJob, JOB_ID))
            db.commit()
        provider = FakeEmbeddingProvider(tracker)
        with pytest.raises(RetrievalIndexNotFound):
            index(tracker, provider)
        assert provider.open_count_at_embed == []
        assert persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "FAILED"])
def test_only_succeeded_jobs_can_be_indexed(status):
    engine, tracker = setup_indexing()
    try:
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).status = status
            db.commit()
        provider = FakeEmbeddingProvider(tracker)
        with pytest.raises(RetrievalIndexNotReady, match="has not succeeded"):
            index(tracker, provider)
        assert provider.open_count_at_embed == []
    finally:
        engine.dispose()


def test_reindex_is_idempotent_and_persists_provenance():
    engine, tracker = setup_indexing()
    try:
        provider = FakeEmbeddingProvider(tracker)
        first = index(tracker, provider)
        first_rows = persisted_rows(tracker)
        second = index(tracker, provider)
        second_rows = persisted_rows(tracker)

        assert first == second
        assert first.chunk_count == first.embedding_count == 1
        assert first_rows == second_rows
        with tracker.session_maker() as db:
            chunk = db.scalar(select(RecordChunk))
            assert (chunk.file_id, chunk.extractor, chunk.extractor_id) == (
                FILE_ID,
                "AUTO_OCR",
                "tesseract:test-revision",
            )
        assert provider.open_count_at_embed == [0, 0]
        assert tracker.open_count == 0
    finally:
        engine.dispose()


def test_changed_result_hash_atomically_replaces_the_old_version():
    engine, tracker = setup_indexing()
    try:
        provider = FakeEmbeddingProvider(tracker)
        first = index(tracker, provider)
        old_rows = persisted_rows(tracker)
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).result_json = json.dumps(
                result_payload("새로 교정된 제주도 가족 여행 기록"), ensure_ascii=False
            )
            approve_job_fixture(db.get(ProcessingJob, JOB_ID))
            db.commit()

        second = index(tracker, provider)
        new_rows = persisted_rows(tracker)
        assert second.source_result_sha256 != first.source_result_sha256
        assert new_rows != old_rows
        assert len(new_rows[0]) == len(new_rows[1]) == 1
        assert new_rows[0][0][3] == "새로 교정된 제주도 가족 여행 기록"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[1.0] * (M2_EMBEDDING_DIMENSIONS - 1)],
        [[math.nan] * M2_EMBEDDING_DIMENSIONS],
        [[math.inf] * M2_EMBEDDING_DIMENSIONS],
        [[0.0] * M2_EMBEDDING_DIMENSIONS],
        [[10**400] * M2_EMBEDDING_DIMENSIONS],
    ],
    ids=["wrong-count", "wrong-dimensions", "nan", "infinity", "zero", "overflow"],
)
def test_invalid_provider_output_publishes_nothing_and_preserves_prior_index(vectors):
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        before = persisted_rows(tracker)
        with pytest.raises(EmbeddingProviderError):
            index(tracker, FakeEmbeddingProvider(tracker, vectors=vectors))
        assert persisted_rows(tracker) == before
        assert tracker.open_count == 0
    finally:
        engine.dispose()


def test_provider_error_publishes_nothing_and_preserves_prior_index():
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        before = persisted_rows(tracker)
        before_search = searchable_ids(tracker)
        assert all(before_search.values())
        _change_result(tracker.session_maker)
        with tracker.session_maker() as db:
            approve_job_fixture(db.get(ProcessingJob, JOB_ID))
            db.commit()
        provider = FakeEmbeddingProvider(tracker, error=OSError("secret provider failure"))
        with pytest.raises(EmbeddingProviderError, match="Embedding provider failed") as caught:
            index(tracker, provider)
        assert "secret" not in str(caught.value)
        assert persisted_rows(tracker) == before
        assert not any(searchable_ids(tracker).values())
        assert provider.open_count_at_embed == [0]
    finally:
        engine.dispose()


def test_http_provider_batches_locally_and_validates_the_complete_response(monkeypatch):
    requests: list[list[str]] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            texts = requests[-1]
            return {
                "model": MODEL_KEY,
                "data": [
                    {"embedding": [float(index + 1)] * M2_EMBEDDING_DIMENSIONS}
                    for index, _ in enumerate(texts)
                ],
            }

    def fake_post(url, *, json, timeout):
        assert url == "http://embedding.test/embed"
        assert json["model"] == MODEL_KEY
        assert timeout == 7
        requests.append(json["input"])
        return Response()

    monkeypatch.setattr("app.retrieval.embeddings.httpx.post", fake_post)
    provider = HTTPEmbeddingProvider(
        endpoint_url="http://embedding.test/embed",
        model_key=MODEL_KEY,
        dimensions=M2_EMBEDDING_DIMENSIONS,
        batch_size=2,
        timeout_seconds=7,
    )
    vectors = provider.embed(["one", "two", "three"])
    assert requests == [["one", "two"], ["three"]]
    assert len(vectors) == 3
    assert all(len(vector) == M2_EMBEDDING_DIMENSIONS for vector in vectors)


@pytest.mark.parametrize("response_model", [None, "bge-m3@wrong-revision"])
def test_http_provider_rejects_absent_or_mismatched_model_identity(monkeypatch, response_model):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            payload = {
                "data": [{"embedding": [1.0] * M2_EMBEDDING_DIMENSIONS}]
            }
            if response_model is not None:
                payload["model"] = response_model
            return payload

    monkeypatch.setattr(
        "app.retrieval.embeddings.httpx.post", lambda *args, **kwargs: Response()
    )
    provider = HTTPEmbeddingProvider(
        endpoint_url="http://embedding.test/embed",
        model_key=MODEL_KEY,
        dimensions=M2_EMBEDDING_DIMENSIONS,
        batch_size=2,
        timeout_seconds=7,
    )
    with pytest.raises(EmbeddingProviderError, match="invalid response"):
        provider.embed(["one"])


@pytest.mark.parametrize(
    "malformed",
    [
        {"extractor": "AUTO_OCR", "extractor_id": "test"},
        {"text": 42, "extractor": "AUTO_OCR", "extractor_id": "test"},
        {"text": "changed", "extractor": "MANUAL", "extractor_id": "test"},
        {"text": "changed", "extractor": "AUTO_OCR", "extractor_id": ""},
        {"text": "changed", "extractor": "AUTO_OCR", "extractor_id": 42},
    ],
    ids=["missing-text", "non-string-text", "wrong-extractor", "empty-extractor-id", "non-string-extractor-id"],
)
def test_malformed_success_result_preserves_prior_complete_index(malformed):
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        before = persisted_rows(tracker)
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).result_json = json.dumps(malformed)
            approve_job_fixture(db.get(ProcessingJob, JOB_ID))
            db.commit()
        with pytest.raises(RetrievalIndexNotReady, match="valid result"):
            index(tracker, FakeEmbeddingProvider(tracker))
        assert persisted_rows(tracker) == before
    finally:
        engine.dispose()


def test_empty_text_is_a_valid_complete_replacement():
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).result_json = json.dumps(result_payload(""))
            approve_job_fixture(db.get(ProcessingJob, JOB_ID))
            db.commit()
        response = index(tracker, FakeEmbeddingProvider(tracker))
        assert response.chunk_count == response.embedding_count == 0
        assert persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()


def _delete_identity(session_maker):
    with session_maker() as db:
        db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
        db.commit()


def _unsafe_delete(session_maker, table, row_id):
    engine = session_maker.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _delete_user(session_maker):
    _unsafe_delete(session_maker, "users", USER_ID)


def _delete_record(session_maker):
    _unsafe_delete(session_maker, "uploads", RECORD_ID)


def _delete_file(session_maker):
    _unsafe_delete(session_maker, "upload_files", FILE_ID)


def _delete_membership(session_maker):
    with session_maker() as db:
        db.query(FamilyMember).filter(
            FamilyMember.room_id == ROOM_ID, FamilyMember.user_id == USER_ID
        ).update({FamilyMember.is_active: False})
        db.commit()


def _delete_job(session_maker):
    with session_maker() as db:
        db.delete(db.get(ProcessingJob, JOB_ID))
        db.commit()


def _change_file(session_maker):
    with session_maker() as db:
        db.get(UploadFile, FILE_ID).upload_id = OTHER_RECORD_ID
        db.commit()


def _change_record(session_maker):
    with session_maker() as db:
        db.get(Upload, RECORD_ID).room_id = OTHER_ROOM_ID
        db.commit()


def _change_attempt(session_maker):
    with session_maker() as db:
        db.get(ProcessingJob, JOB_ID).attempt = 2
        db.commit()


def _change_result(session_maker):
    with session_maker() as db:
        db.get(ProcessingJob, JOB_ID).result_json = json.dumps(
            result_payload("임베딩 중 바뀐 결과"), ensure_ascii=False
        )
        db.commit()


def _change_status(session_maker):
    with session_maker() as db:
        db.get(ProcessingJob, JOB_ID).status = "FAILED"
        db.commit()


@pytest.mark.parametrize(
    ("mutation", "error_type"),
    [
        (_delete_identity, PrincipalNotPersisted),
        (_delete_user, PrincipalNotPersisted),
        (_delete_membership, RetrievalIndexStale),
        (_change_record, RetrievalIndexStale),
        (_delete_record, RetrievalIndexStale),
        (_change_file, RetrievalIndexStale),
        (_delete_file, RetrievalIndexStale),
        (_delete_job, RetrievalIndexStale),
        (_change_attempt, RetrievalIndexStale),
        (_change_result, RetrievalIndexStale),
        (_change_status, RetrievalIndexStale),
    ],
    ids=[
        "identity",
        "user",
        "membership",
        "record-linkage",
        "record-removal",
        "file-linkage",
        "file-removal",
        "job",
        "attempt",
        "result",
        "status",
    ],
)
def test_changes_during_embedding_are_rejected_without_partial_publication(mutation, error_type):
    engine, tracker = setup_indexing()
    try:
        provider = FakeEmbeddingProvider(tracker, mutation=mutation)
        with pytest.raises(error_type):
            index(tracker, provider)
        assert persisted_rows(tracker) == ([], [])
        assert provider.open_count_at_embed == [0]
        assert tracker.open_count == 0
    finally:
        engine.dispose()


def test_transaction_b_failure_rolls_back_and_preserves_the_prior_complete_version():
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        before = persisted_rows(tracker)
        before_search = searchable_ids(tracker)
        assert all(before_search.values())
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).result_json = json.dumps(
                result_payload("commit 직전에 실패하는 새 결과"), ensure_ascii=False
            )
            approve_job_fixture(db.get(ProcessingJob, JOB_ID))
            db.commit()

        class FailSecondCommit:
            def __init__(self):
                self.calls = 0

            def __call__(self):
                db = tracker()
                self.calls += 1
                if self.calls == 2:
                    def fail_commit():
                        db.flush()
                        raise RuntimeError("commit failed")

                    db.commit = fail_commit
                return db

        with pytest.raises(RuntimeError, match="commit failed"):
            index_processing_result(
                record_id=RECORD_ID,
                processing_job_id=JOB_ID,
                user_id=USER_ID,
                provider=FakeEmbeddingProvider(tracker),
                session_factory=FailSecondCommit(),
            )
        assert persisted_rows(tracker) == before
        assert not any(searchable_ids(tracker).values())
        assert tracker.open_count == 0
    finally:
        engine.dispose()


def test_source_change_immediately_before_transaction_b_write_is_rejected(tmp_path):
    engine, tracker = setup_file_indexing(tmp_path)

    class InterleavingFactory:
        def __init__(self):
            self.calls = 0
            self.fired = False

        def __call__(self):
            db = tracker()
            self.calls += 1
            if self.calls == 2:
                original_execute = db.execute

                def execute(statement, *args, **kwargs):
                    if not self.fired and (
                        getattr(statement, "is_update", False)
                        or getattr(statement, "is_delete", False)
                    ):
                        self.fired = True
                        _change_result(tracker.session_maker)
                    return original_execute(statement, *args, **kwargs)

                db.execute = execute
            return db

    factory = InterleavingFactory()
    try:
        with pytest.raises(RetrievalIndexStale):
            index_processing_result(
                record_id=RECORD_ID,
                processing_job_id=JOB_ID,
                user_id=USER_ID,
                provider=FakeEmbeddingProvider(tracker),
                session_factory=factory,
            )
        assert factory.fired
        assert persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()


def test_simultaneous_same_version_indexing_is_idempotent(tmp_path):
    engine, tracker = setup_file_indexing(tmp_path)
    embed_barrier = threading.Barrier(2)
    write_barrier = threading.Barrier(2)

    class ConcurrentProvider:
        model_key = MODEL_KEY
        dimensions = M2_EMBEDDING_DIMENSIONS

        def embed(self, texts):
            embed_barrier.wait(timeout=10)
            return [[1.0] * self.dimensions for _ in texts]

    class ConcurrentFactory:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def __call__(self):
            db = tracker()
            with self.lock:
                self.calls += 1
                transaction_b = self.calls > 2
            if transaction_b:
                original_execute = db.execute
                waiting = False

                def execute(statement, *args, **kwargs):
                    nonlocal waiting
                    if not waiting and (
                        getattr(statement, "is_update", False)
                        or getattr(statement, "is_delete", False)
                    ):
                        waiting = True
                        write_barrier.wait(timeout=10)
                    return original_execute(statement, *args, **kwargs)

                db.execute = execute
            return db

    factory = ConcurrentFactory()
    try:
        def run():
            return index_processing_result(
                record_id=RECORD_ID,
                processing_job_id=JOB_ID,
                user_id=USER_ID,
                provider=ConcurrentProvider(),
                session_factory=factory,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = [future.result(timeout=20) for future in [executor.submit(run), executor.submit(run)]]
        assert responses[0] == responses[1]
        chunks, embeddings = persisted_rows(tracker)
        assert len(chunks) == len(embeddings) == 1
    finally:
        engine.dispose()
