from __future__ import annotations

import json

from fastapi.testclient import TestClient

from test_retrieval_indexing import (
    JOB_ID,
    OTHER_JOB_ID,
    OTHER_RECORD_ID,
    RECORD_ID,
    USER_ID,
    FakeEmbeddingProvider,
    persisted_rows,
    setup_indexing,
)
from app.api import deps
from app.api.routes import retrieval as retrieval_routes
from app.main import app
from app.models.auth_identity import AuthIdentity
from app.models.family import FamilyMember
from app.models.processing_job import ProcessingJob


def setup_api():
    from app.core import database
    engine, tracker = setup_indexing()
    engine._previous_audit_engine = database.engine
    database.engine = engine
    app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(
        user_id=USER_ID
    )
    old_session_local = retrieval_routes.SessionLocal
    old_provider = retrieval_routes.EMBEDDING_PROVIDER
    retrieval_routes.SessionLocal = tracker
    retrieval_routes.EMBEDDING_PROVIDER = FakeEmbeddingProvider(tracker)
    return engine, tracker, old_session_local, old_provider


def teardown_api(engine, old_session_local, old_provider):
    from app.core import database
    database.engine = engine._previous_audit_engine
    retrieval_routes.SessionLocal = old_session_local
    retrieval_routes.EMBEDDING_PROVIDER = old_provider
    app.dependency_overrides.clear()
    engine.dispose()


def post_index(client, record_id=RECORD_ID, job_id=JOB_ID):
    return client.post(
        f"/api/v1/records/{record_id}/retrieval-index",
        json={"processing_job_id": job_id},
    )


def post_search(client, *, arm="vector", query="summer", k=5):
    return client.post(
        "/api/v1/families/room-ri-1/retrieval/search",
        json={"query": query, "arm": arm, "k": k},
    )


def test_index_endpoint_returns_public_source_version_and_counts():
    engine, tracker, old_sl, old_provider = setup_api()
    try:
        response = post_index(TestClient(app))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source_attempt"] == 1
        assert len(body["source_result_sha256"]) == 64
        assert body["chunk_count"] == body["embedding_count"] == 1
        assert body["chunker_version"] == "ws160-o32-v1"
        assert body["model_key"] == "bge-m3@test-revision"
        assert tracker.open_count == 0
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_missing_and_foreign_index_sources_have_identical_404_responses():
    engine, tracker, old_sl, old_provider = setup_api()
    try:
        client = TestClient(app)
        missing = post_index(client, "missing", "missing")
        foreign = post_index(client, OTHER_RECORD_ID, OTHER_JOB_ID)
        assert (missing.status_code, missing.json()) == (foreign.status_code, foreign.json())
        assert missing.status_code == 404
        assert persisted_rows(tracker) == ([], [])
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_missing_and_foreign_records_match_with_the_same_authorized_job():
    engine, tracker, old_sl, old_provider = setup_api()
    try:
        client = TestClient(app)
        missing = post_index(client, "missing", JOB_ID)
        foreign = post_index(client, OTHER_RECORD_ID, JOB_ID)
        assert (missing.status_code, missing.json()) == (foreign.status_code, foreign.json())
        assert missing.status_code == 404
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_index_endpoint_rejects_unpersisted_principal_before_provider():
    engine, tracker, old_sl, old_provider = setup_api()
    try:
        with tracker.session_maker() as db:
            db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
            db.commit()
        response = post_index(TestClient(app))
        assert response.status_code == 401
        assert response.json() == {"detail": "Authenticated user not found"}
        assert persisted_rows(tracker) == ([], [])
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_index_endpoint_refuses_unsuccessful_job():
    engine, tracker, old_sl, old_provider = setup_api()
    try:
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).status = "FAILED"
            db.commit()
        response = post_index(TestClient(app))
        assert response.status_code == 409
        assert response.json() == {"detail": "Processing job has not succeeded"}
        assert persisted_rows(tracker) == ([], [])
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_index_endpoint_hides_provider_failure_and_publishes_nothing():
    engine, tracker, old_sl, old_provider = setup_api()
    retrieval_routes.EMBEDDING_PROVIDER = FakeEmbeddingProvider(
        tracker, error=RuntimeError("http://secret-host/internal")
    )
    try:
        response = post_index(TestClient(app))
        assert response.status_code == 503
        assert response.json() == {"detail": "Embedding provider failed"}
        assert "secret-host" not in response.text
        assert persisted_rows(tracker) == ([], [])
        assert tracker.open_count == 0
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_index_endpoint_maps_numeric_overflow_to_safe_503():
    engine, tracker, old_sl, old_provider = setup_api()
    retrieval_routes.EMBEDDING_PROVIDER = FakeEmbeddingProvider(
        tracker, vectors=[[10**400] * 1024]
    )
    try:
        response = post_index(TestClient(app))
        assert response.status_code == 503
        assert response.json() == {"detail": "Embedding provider failed"}
        assert persisted_rows(tracker) == ([], [])
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_index_endpoint_rejects_result_changed_during_embedding():
    engine, tracker, old_sl, old_provider = setup_api()

    def change_result(session_maker):
        with session_maker() as db:
            db.get(ProcessingJob, JOB_ID).result_json = json.dumps(
                {"text": "changed", "extractor": "AUTO_OCR", "extractor_id": "test"}
            )
            db.commit()

    retrieval_routes.EMBEDDING_PROVIDER = FakeEmbeddingProvider(tracker, mutation=change_result)
    try:
        response = post_index(TestClient(app))
        assert response.status_code == 409
        assert response.json() == {"detail": "Retrieval source changed during indexing"}
        assert persisted_rows(tracker) == ([], [])
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_vector_search_embeds_outside_sessions_and_returns_room_scoped_hit():
    # Holding Transaction A open across query embedding would make the last count nonzero.
    engine, tracker, old_sl, old_provider = setup_api()
    try:
        client = TestClient(app)
        assert post_index(client).status_code == 200
        response = post_search(client)
        assert response.status_code == 200, response.text
        assert [hit["chunk_id"] for hit in response.json()["hits"]]
        assert retrieval_routes.EMBEDDING_PROVIDER.open_count_at_embed[-1] == 0
        assert tracker.open_count == 0
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_vector_search_refuses_membership_removed_during_query_embedding():
    # Removing Transaction B membership revalidation would return a stale successful search.
    engine, tracker, old_sl, old_provider = setup_api()

    def remove_membership(session_maker):
        with session_maker() as db:
            db.query(FamilyMember).filter(
                FamilyMember.room_id == "room-ri-1", FamilyMember.user_id == USER_ID
            ).update({FamilyMember.is_active: False})
            db.commit()

    retrieval_routes.EMBEDDING_PROVIDER = FakeEmbeddingProvider(tracker, mutation=remove_membership)
    try:
        response = post_search(TestClient(app))
        assert response.status_code == 404
        assert response.json() == {"detail": "Retrieval room not found"}
        assert retrieval_routes.EMBEDDING_PROVIDER.open_count_at_embed == [0]
        assert tracker.open_count == 0
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_vector_search_maps_principal_loss_before_transaction_a_to_401():
    # Letting require_persisted_principal escape turns an authentication refusal into a 500.
    engine, tracker, old_sl, old_provider = setup_api()
    try:
        with tracker.session_maker() as db:
            db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
            db.commit()
        response = post_search(TestClient(app, raise_server_exceptions=False))
        assert response.status_code == 401
        assert response.json() == {"detail": "Authenticated user not found"}
        assert retrieval_routes.EMBEDDING_PROVIDER.open_count_at_embed == []
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_vector_search_maps_principal_loss_during_embedding_to_401():
    # Removing the Transaction B principal mapping turns this race into a 500.
    engine, tracker, old_sl, old_provider = setup_api()

    def remove_identity(session_maker):
        with session_maker() as db:
            db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
            db.commit()

    retrieval_routes.EMBEDDING_PROVIDER = FakeEmbeddingProvider(tracker, mutation=remove_identity)
    try:
        response = post_search(TestClient(app, raise_server_exceptions=False))
        assert response.status_code == 401
        assert response.json() == {"detail": "Authenticated user not found"}
        assert retrieval_routes.EMBEDDING_PROVIDER.open_count_at_embed == [0]
        assert tracker.open_count == 0
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_vector_provider_failure_returns_safe_503_without_lexical_fallback():
    # Replacing the provider error with a lexical result leaks a different arm's behavior.
    engine, tracker, old_sl, old_provider = setup_api()
    retrieval_routes.EMBEDDING_PROVIDER = FakeEmbeddingProvider(
        tracker, error=RuntimeError("http://secret-host/internal")
    )
    try:
        response = post_search(TestClient(app), arm="hybrid_rrf")
        assert response.status_code == 503
        assert response.json() == {"detail": "Embedding provider failed"}
        assert "secret-host" not in response.text
        assert tracker.open_count == 0
    finally:
        teardown_api(engine, old_sl, old_provider)
