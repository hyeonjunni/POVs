import hashlib
import json

import pytest
from app.ai.grounded_answering import SyntheticPlanProvider
from app.api.routes import grounded_answering, retrieval
from app.models.processing_job import ProcessingJob
from app.models.retrieval import RecordChunk, RecordEmbedding
from app.retrieval.evaluation import SyntheticTokenHashProvider
from sqlalchemy import select
from test_processing_api import ROOM_ID
from test_upload_immutable_publication import case as _case

QUOTE = "아버지 검진은 오후 2시였다."
case = _case


@pytest.fixture
def native_legacy(case, monkeypatch):
    client, tracker, _ = case
    embedding = SyntheticTokenHashProvider()
    for route in (retrieval, grounded_answering):
        monkeypatch.setattr(route, "SessionLocal", tracker)
        monkeypatch.setattr(route, "EMBEDDING_PROVIDER", embedding)
    monkeypatch.setattr(grounded_answering, "get_provider_for_model", lambda _: SyntheticPlanProvider())
    created = client.post("/api/v1/uploads", json={"room_id": ROOM_ID, "type": "text",
        "title": "Synthetic text", "description": QUOTE, "tags": ["synthetic"]})
    assert created.status_code == 200, created.text
    rid = created.json()["upload_id"]
    published = client.post(f"/api/v1/uploads/{rid}/publish-text")
    assert published.status_code == 200, published.text
    queued = client.post(f"/api/v1/records/{rid}/processing-jobs", json={
        "upload_file_id": published.json()["file_id"], "processor": "text"})
    assert queued.status_code == 200, queued.text
    jid = queued.json()["job_id"]
    ran = client.post(f"/api/v1/processing-jobs/{jid}/run")
    assert ran.status_code == 200 and ran.json()["status"] == "SUCCEEDED", ran.text
    return client, tracker, rid, jid


def approve_and_index(client, rid, jid):
    reviewed = client.post(f"/api/v1/processing-jobs/{jid}/review", json={"action": "approve"})
    assert reviewed.status_code == 200, reviewed.text
    indexed = client.post(f"/api/v1/records/{rid}/retrieval-index", json={"processing_job_id": jid})
    assert indexed.status_code == 200, indexed.text
    return indexed.json()


def ask(client, query="아버지 검진은 몇 시였어?"):
    return client.post(f"/api/v1/families/{ROOM_ID}/ask", json={
        "query": query, "arm": "hybrid_rrf", "k": 5, "model_id": "exaone-45-33b-dgx"})


def test_legacy_text_review_is_required(native_legacy):
    client, _, rid, jid = native_legacy
    response = client.post(f"/api/v1/records/{rid}/retrieval-index", json={"processing_job_id": jid})
    assert response.status_code == 409
    assert ask(client).json()["gate_route"] == "NO_RECORD"


def test_legacy_text_commits_exact_source_and_vectors(native_legacy):
    client, tracker, rid, jid = native_legacy
    committed = approve_and_index(client, rid, jid)
    assert committed["chunk_count"] > 0
    assert committed["embedding_count"] == committed["chunk_count"]
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob, jid)
        result = json.loads(job.result_json)
        assert result["extractor"] == "NATIVE_TEXT"
        assert result["source_sha256"] == hashlib.sha256(QUOTE.encode()).hexdigest()
        chunks = db.scalars(select(RecordChunk).where(RecordChunk.record_id == rid)).all()
        vectors = db.scalars(select(RecordEmbedding).where(RecordEmbedding.chunk_id.in_([c.id for c in chunks]))).all()
        assert chunks and vectors
        assert all(v.dimensions == len(v.embedding) == 1024 for v in vectors)


@pytest.mark.parametrize("query", ["아버지 검진은 몇 시였어?", "아버지 검진 몇 시야?"])
def test_legacy_native_answer_cites_readable_exact_source(native_legacy, query):
    client, _, rid, jid = native_legacy
    approve_and_index(client, rid, jid)
    response = ask(client, query)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["gate_route"] == "ANSWER", answer
    citation = answer["citations"][0]
    assert citation["record_id"] == rid
    assert citation["extractor"] == "NATIVE_TEXT"
    original = client.get(citation["original_url"])
    assert original.status_code == 200 and original.content.decode() == QUOTE


def test_legacy_native_delete_removes_retrieval(native_legacy):
    client, _, rid, jid = native_legacy
    approve_and_index(client, rid, jid)
    deleted = client.delete(f"/api/v1/uploads/{rid}")
    assert deleted.status_code == 200, deleted.text
    assert ask(client).json()["gate_route"] == "NO_RECORD"
