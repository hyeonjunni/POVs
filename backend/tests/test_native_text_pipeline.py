import json

import pytest
from app.ai.grounded_answering import SyntheticPlanProvider
from app.retrieval.evaluation import SyntheticTokenHashProvider
from sqlalchemy import text
from test_secure_catalogue import catalogue as _catalogue
from test_secure_identity import identity_client as _identity_client
from test_secure_media import media_client as _media_client
from test_secure_retrieval import ask_secure, index_secure, search_secure

QUOTE = "아버지 검진은 오후 2시였다."
catalogue = _catalogue
identity_client = _identity_client
media_client = _media_client


@pytest.fixture
def native_case(media_client):
    client = media_client
    runtime = client.app.state.secure_runtime
    runtime.embedding_provider = SyntheticTokenHashProvider()
    runtime.inference_provider = SyntheticPlanProvider()
    created = client.request("POST", "/uploads", headers=client.auth, json={
        "room_id": client.room["room_id"], "type": "text",
        "title": "Synthetic appointment", "description": QUOTE,
    })
    assert created.status_code == 200, created.text
    rid = created.json()["upload_id"]
    published = client.request("POST", f"/uploads/{rid}/publish-text", headers=client.auth)
    assert published.status_code == 200, published.text
    row = published.json()
    queued = client.request("POST", f"/records/{rid}/processing-jobs", headers=client.auth,
        json={"upload_file_id": row["file_id"], "processor": "text"})
    assert queued.status_code == 200, queued.text
    jid = queued.json()["job_id"]
    ran = client.request("POST", f"/processing-jobs/{jid}/run", headers=client.auth)
    assert ran.status_code == 200 and ran.json()["status"] == "SUCCEEDED", ran.text
    return client, row, ran.json()


@pytest.fixture
def indexed_native(native_case):
    client, row, done = native_case
    response = client.request("POST", f"/processing-jobs/{done['job_id']}/review",
        headers=client.auth, json={"action": "approve"})
    assert response.status_code == 200, response.text
    committed = index_secure(client, row, done)
    assert committed.status_code == 200, committed.text
    return client, row, done, committed.json()


def test_plain_text_source_can_enter_processing_lifecycle(media_client):
    # Given: an ordinary native text record, not a fabricated OCR job.
    client = media_client
    created = client.request("POST", "/uploads", headers=client.auth, json={
        "room_id": client.room["room_id"], "type": "text",
        "title": "Synthetic appointment", "description": "아버지 검진은 오후 2시였다.",
    })
    assert created.status_code == 200, created.text
    record_id = created.json()["upload_id"]

    # When: publishing the actual UTF-8 source into the existing binary lifecycle.
    source = client.request("POST", f"/uploads/{record_id}/binary", headers=client.auth,
        files={"file": ("source.txt", "아버지 검진은 오후 2시였다.".encode(), "text/plain")})

    # Then: the source has a real stable file identity usable by native processing.
    assert source.status_code == 200, source.text
    assert source.json()["file_id"]


def test_unapproved_native_text_is_not_evidence(native_case):
    client, row, done = native_case
    response = index_secure(client, row, done)
    assert response.status_code in (409, 503)
    assert search_secure(client).json()["hits"] == []
    answer = ask_secure(client).json()
    assert answer["gate_route"] == "NO_RECORD"
    assert answer["provider_mode"] == "not_called"


def test_native_index_commits_chunks_and_synthetic_1024d_vectors(indexed_native):
    client, row, done, committed = indexed_native
    assert committed["chunk_count"] > 0
    assert committed["embedding_count"] == committed["chunk_count"]
    from app.services.secure_retrieval import materialize_room
    corpus = materialize_room(client.app.state.secure_runtime,
        client.session["user"]["user_id"], client.room["room_id"])
    assert corpus.chunks and all(c.extractor == "NATIVE_TEXT" for c in corpus.chunks)
    assert corpus.vectors and all(len(vector) == 1024 for _, vector in corpus.vectors)
    with client.engine.connect() as db:
        assert db.scalar(text("SELECT state FROM protected_pointers WHERE owner_type='record_index' AND owner_id=:r"), {"r": row["upload_id"]}) == "ENCRYPTED_ACTIVE"
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state='COMMITTED' AND owner_type='record_index' AND owner_id=:r"), {"r": row["upload_id"]}) > 0
        assert db.scalar(text("SELECT result_json FROM processing_jobs WHERE id=:j"), {"j": done["job_id"]}) is None
    assert all(QUOTE.encode() not in blob for _, blob in client.objects.values())


@pytest.mark.parametrize("query", ["아버지 검진은 몇 시였어?", "아버지 검진 몇 시야?"])
def test_native_relevant_query_is_admitted_with_valid_citation(indexed_native, query):
    client, row, _, _ = indexed_native
    response = ask_secure(client, query)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["gate_route"] == "ANSWER", answer
    citation = answer["citations"][0]
    assert citation["record_id"] == row["upload_id"]
    assert citation["extractor"] == "NATIVE_TEXT"
    original = client.request("GET", citation["original_url"].removeprefix("/api/v1"), headers=client.auth)
    assert original.status_code == 200 and original.content.decode() == QUOTE


def test_unrelated_native_query_skips_inference(indexed_native, monkeypatch):
    client, _, _, _ = indexed_native
    from app.ai import grounded_answering
    monkeypatch.setattr(grounded_answering.SyntheticPlanProvider, "generate",
        lambda *args, **kwargs: pytest.fail("NO_RECORD invoked inference"))
    response = ask_secure(client, "우주선 연료 비용은 얼마야?")
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "NO_RECORD"
    assert response.json()["provider_mode"] == "not_called"
    assert response.json()["citations"] == []


def test_tombstoned_native_text_leaves_search(indexed_native):
    client, row, _, _ = indexed_native
    deleted = client.request("DELETE", f"/uploads/{row['upload_id']}", headers=client.auth)
    assert deleted.status_code == 200, deleted.text
    assert search_secure(client).json()["hits"] == []
    assert ask_secure(client).json()["gate_route"] == "NO_RECORD"


def test_foreign_family_cannot_retrieve_native_text(indexed_native):
    client, _, _, _ = indexed_native
    from test_secure_identity import create_room
    stranger, _, _ = client.signup("stranger", "stranger@example.com")
    foreign_room, _ = create_room(client, stranger)
    client.auth = {"Authorization": "Bearer " + stranger["access_token"]}
    denied = search_secure(client)
    assert denied.status_code in (403, 404, 503)
    client.room = foreign_room
    assert search_secure(client).json()["hits"] == []
    assert ask_secure(client).json()["gate_route"] == "NO_RECORD"


def test_replacing_native_source_invalidates_old_citation(indexed_native):
    client, row, _, _ = indexed_native
    old = ask_secure(client).json()["citations"][0]["original_url"]
    replacement = client.request("POST", f"/uploads/{row['upload_id']}/binary", headers=client.auth,
        files={"file": ("changed.txt", "아버지 검진은 오후 4시였다.".encode(), "text/plain")})
    assert replacement.status_code == 200, replacement.text
    response = client.request("GET", old.removeprefix("/api/v1"), headers=client.auth)
    assert response.status_code == 409, response.text
    assert search_secure(client).json()["hits"] == []


def test_native_trace_is_typed_without_plaintext(indexed_native, monkeypatch):
    client, row, _, _ = indexed_native
    from app.core import demo_trace
    events = []
    monkeypatch.setenv("AMBIENT_DEMO_TRACE_SOCKET", "/unused")
    monkeypatch.setattr(demo_trace, "_send", events.append)
    assert ask_secure(client).json()["gate_route"] == "ANSWER"
    assert any(event.get("source_type") == "TEXT" for event in events)
    raw = json.dumps(events, ensure_ascii=False)
    assert all(value not in raw for value in (QUOTE, row["upload_id"], client.session["access_token"]))
