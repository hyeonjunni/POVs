"""Real M2 indexing/search; only embedding and inference transports are faked."""
from __future__ import annotations

import json
from ocr_approval_helpers import confirmed_review, approve_job_fixture

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.models.auth_identity import AuthIdentity
from app.models.family import FamilyMember
from app.models.processing_job import ProcessingJob
from app.models.retrieval import RecordChunk
from app.models.upload import Upload, UploadFile
from app.ai.providers.base import InferenceResponse
from app.retrieval.chunking import ChunkSource, chunk_processing_result
from app.services.retrieval import search_room
from app.services.retrieval_indexing import index_processing_result
from test_retrieval_indexing import (
    FILE_ID, JOB_ID, OTHER_JOB_ID, OTHER_RECORD_ID, OTHER_ROOM_ID, OTHER_USER_ID,
    RECORD_ID, ROOM_ID, USER_ID, FakeEmbeddingProvider, result_payload, setup_indexing,
)

QUOTE = "아버지 검진은 오후 2시였다."
OTHER_QUOTE = "아버지 검진은 오전 9시였다."
PLACE_QUOTE = "2026년 가족 여행에서 우리는 강릉 바다를 함께 걸었다."


@pytest.mark.parametrize("mode", ["selected", "fallback", "stale"])
def test_public_ask_response_excludes_internal_and_rejected_data(rig, mode):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    if mode == "fallback":
        provider.raw = '{"answer":"rejected-private-output"}'
    elif mode == "stale":
        provider.mutation = lambda _sessions: set_result(tracker, "changed")
    response = ask(client)
    assert response.status_code == (409 if mode == "stale" else 200)
    forbidden = {"raw_output", "prompt", "scores", "storage_path", "source_result_sha256"}

    def check(value):
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(response.json())
    assert "rejected-private-output" not in response.text


class FakeInference:
    def __init__(self, tracker):
        self.tracker = tracker
        self.raw = '{"claim_ids":["claim-1"]}'
        self.error = None
        self.mutation = None
        self.calls = 0
        self.open_count_at_generate = []
        self.factory_calls = []

    def generate(self, request):
        self.calls += 1
        self.open_count_at_generate.append(self.tracker.open_count)
        if self.mutation:
            self.mutation(self.tracker.session_maker)
        if self.error:
            raise self.error
        return InferenceResponse(provider="fake", model_id=request.model_id, output_text=self.raw, mode="fake")


@pytest.fixture
def rig(monkeypatch):
    from app.api.routes import grounded_answering as route
    from app.core import database

    engine, tracker = setup_indexing()
    monkeypatch.setattr(database, "engine", engine)
    embedding = FakeEmbeddingProvider(tracker)
    provider = FakeInference(tracker)
    monkeypatch.setattr(route, "SessionLocal", tracker)
    monkeypatch.setattr(route, "EMBEDDING_PROVIDER", embedding)
    def provider_factory(model_id):
        provider.factory_calls.append(model_id)
        return provider
    monkeypatch.setattr(route, "get_provider_for_model", provider_factory)
    app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=USER_ID)
    try:
        yield tracker, embedding, provider, TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def set_result(tracker, text=QUOTE, *, job_id=JOB_ID):
    with tracker.session_maker() as db:
        db.get(ProcessingJob, job_id).result_json = json.dumps(result_payload(text), ensure_ascii=False)
        approve_job_fixture(db.get(ProcessingJob, job_id))
        db.commit()


def index(tracker, embedding, *, record_id=RECORD_ID, job_id=JOB_ID, user_id=USER_ID):
    index_processing_result(record_id=record_id, processing_job_id=job_id, user_id=user_id,
                            provider=embedding, session_factory=tracker)


def add_second_source(tracker, text=OTHER_QUOTE):
    from app.services.record_policy import register_legacy_record
    with tracker.session_maker() as db:
        db.add(Upload(id="record-ask-2", room_id=ROOM_ID, uploader_user_id=USER_ID,
                      type="image", title="추가 기록", status="uploaded"))
        db.flush()
        db.add(UploadFile(id="file-ask-2", upload_id="record-ask-2", storage_bucket="local-media",
                          storage_path="second.png", mime_type="image/png", file_uploader_user_id=USER_ID))
        register_legacy_record(db, "record-ask-2")
        db.flush()
        db.add(ProcessingJob(id="job-ask-2", record_id="record-ask-2", upload_file_id="file-ask-2",
                             room_id=ROOM_ID, processor="ocr", status="SUCCEEDED", attempt=1,
                             result_json=json.dumps(result_payload(text), ensure_ascii=False),
                             review_json=confirmed_review(result_payload(text))))
        db.commit()


def add_safety_sources(tracker, embedding, sources):
    from app.services.record_policy import register_legacy_record
    with tracker.session_maker() as db:
        for job_id, text in sources:
            record_id = f"record-{job_id}"
            file_id = f"file-{job_id}"
            db.add(Upload(id=record_id, room_id=ROOM_ID, uploader_user_id=USER_ID,
                          type="image", title="safety", status="uploaded"))
            db.flush()
            db.add(UploadFile(id=file_id, upload_id=record_id, storage_bucket="local-media",
                              storage_path=f"{job_id}.png", mime_type="image/png", file_uploader_user_id=USER_ID))
            register_legacy_record(db, record_id)
            db.flush()
            db.add(ProcessingJob(id=job_id, record_id=record_id, upload_file_id=file_id,
                                 room_id=ROOM_ID, processor="ocr", status="SUCCEEDED", attempt=1,
                                 result_json=json.dumps(result_payload(text), ensure_ascii=False),
                                 review_json=confirmed_review(result_payload(text))))
        db.commit()
    for job_id, _text in sources:
        index(tracker, embedding, record_id=f"record-{job_id}", job_id=job_id)


def safety_chunk_id(job_id, text):
    return chunk_processing_result(ChunkSource(
        room_id=ROOM_ID, record_id="unused-record", file_id="unused-file",
        processing_job_id=job_id, source_attempt=1, result=result_payload(text),
    ))[0].id


def ask(client, query="아버지 검진은 몇 시였어?", *, room_id=ROOM_ID, k=1,
        model_id="exaone-45-33b-dgx", arm="vector"):
    return client.post(f"/api/v1/families/{room_id}/ask", json={
        "query": query, "model_id": model_id, "arm": arm, "k": k,
    })


def test_safety_candidate_boundary_49_50_51(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    add_safety_sources(tracker, embedding, [
        (f"job-safety-boundary-{number:03d}", QUOTE) for number in range(48)
    ])

    assert ask(client, arm="bm25").json()["gate_route"] == "ANSWER"
    add_safety_sources(tracker, embedding, [("job-safety-boundary-049", QUOTE)])
    assert ask(client, arm="bm25").json()["gate_route"] == "ANSWER"

    add_safety_sources(tracker, embedding, [("job-safety-boundary-050", QUOTE)])
    calls_before_limit = provider.calls
    response = ask(client, arm="bm25")
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "CLARIFY"
    assert response.json()["provider_mode"] == "not_called"
    assert provider.calls == calls_before_limit


def test_conflict_inside_complete_50_candidate_pool(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    add_safety_sources(tracker, embedding, [
        *((f"job-safety-conflict-{number:03d}", QUOTE) for number in range(48)),
        ("job-safety-conflict-inside", OTHER_QUOTE),
    ])

    response = ask(client, arm="bm25")
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "CONFLICT"
    assert provider.calls == 0
    assert provider.factory_calls == []


def test_51st_contradiction_clarifies_for_every_public_k_without_inference(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    same_sources = [(f"job-safety-last-{number:03d}", QUOTE) for number in range(49)]
    add_safety_sources(tracker, embedding, same_sources)
    last_same_id = max([
        safety_chunk_id(JOB_ID, QUOTE),
        *(safety_chunk_id(job_id, text) for job_id, text in same_sources),
    ])
    conflict_job_id = next(
        job_id for number in range(10_000)
        if safety_chunk_id(job_id := f"job-safety-last-conflict-{number:04d}", OTHER_QUOTE) > last_same_id
    )
    add_safety_sources(tracker, embedding, [(conflict_job_id, OTHER_QUOTE)])
    ranked = search_room(
        room_id=ROOM_ID, user_id=USER_ID, query="아버지 검진은 몇 시였어?", arm="bm25", k=51,
        provider=embedding, session_factory=tracker, candidate_limit=200,
    )
    assert len(ranked) == 51
    assert ranked[-1].chunk.text == OTHER_QUOTE

    for k in (1, 2, 5):
        response = ask(client, arm="bm25", k=k)
        assert response.status_code == 200, response.text
        assert response.json()["gate_route"] == "CLARIFY"
        assert response.json()["provider_mode"] == "not_called"
    assert provider.calls == 0
    assert provider.factory_calls == []


def test_answer_uses_exact_indexed_quote_citation_and_no_open_session(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    response = ask(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gate_route"] == "ANSWER"
    assert body["answer"] == "기록에서 추출된 내용\n" + QUOTE
    assert body["gate_action"] == "literal_render"
    assert body["provider_mode"] == "selected"
    assert body["citations"][0]["record_id"] == RECORD_ID
    assert body["citations"][0]["evidence_status"] == "extracted"
    assert body["citations"][0]["chunk_char_start"] == 0
    assert body["citations"][0]["chunk_char_end"] == len(QUOTE)
    assert provider.open_count_at_generate == [0]
    assert tracker.open_count == 0
    assert "raw_output" not in body and "source_result_sha256" not in body
    assert client.post(f"/api/v1/ai/families/{ROOM_ID}/ask", json={}).status_code == 404


def test_grounded_place_evidence_advances_through_ask_gate(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker, PLACE_QUOTE)
    index(tracker, embedding)

    response = ask(client, "2026년 가족 여행에서 어디를 걸었어?")

    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "ANSWER"
    assert response.json()["answer"] == "기록에서 추출된 내용\n" + PLACE_QUOTE
    assert provider.calls == 1


@pytest.mark.parametrize("raw", [
    "invented prose", "not json", '{"claim_ids":["claim-99"]}',
    '{"claim_ids":["claim-1","claim-1"]}', '{"claim_ids":[]}',
    '{"claim_ids":["claim-1"],"answer":"private output"}',
])
def test_invalid_selection_falls_back_to_complete_literal_plan(rig, raw):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    provider.raw = raw
    response = ask(client)
    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "기록에서 추출된 내용\n" + QUOTE
    assert response.json()["provider_mode"] == "fallback"
    assert raw not in response.text


def test_provider_error_falls_back_without_exposing_error(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    provider.error = RuntimeError("http://private-provider/error")
    response = ask(client)
    assert response.status_code == 200
    assert response.json()["provider_mode"] == "fallback"
    assert response.json()["answer"] == "기록에서 추출된 내용\n" + QUOTE
    assert "private-provider" not in response.text


def test_complete_permutation_changes_only_quote_and_citation_order(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    add_second_source(tracker, "아버지 검진은 오후 2시로 확정되었다.")
    index(tracker, embedding)
    index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    provider.raw = '{"claim_ids":["claim-2","claim-1"]}'
    response = ask(client, "아버지 검진은 몇 시였어?", k=2)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "ANSWER"
    assert response.json()["provider_mode"] == "selected"
    assert response.json()["answer"] == "기록에서 추출된 내용\n아버지 검진은 오후 2시로 확정되었다.\n" + QUOTE
    assert [c["record_id"] for c in response.json()["citations"]] == ["record-ask-2", RECORD_ID]


@pytest.mark.parametrize("query,text,expected", [
    ("부산 여행의 정확한 날짜는?", QUOTE, "NO_RECORD"),
    ("할머니는 몇 시였어?", QUOTE, "NO_RECORD"),
    ("아버지 검진은 몇 시였어?", None, "CONFLICT"),
    ("아버지 검진은 몇 시였어?", "", "NO_RECORD"),
])
def test_insufficient_routes_never_construct_or_call_provider(rig, query, text, expected):
    tracker, embedding, provider, client = rig
    set_result(tracker, QUOTE if text is None else text)
    index(tracker, embedding)
    if text is None:
        add_second_source(tracker)
        index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    response = ask(client, query, k=2)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == expected
    assert response.json()["gate_action"] == "llm_not_called"
    assert response.json()["citations"] == []
    assert provider.calls == 0
    assert provider.factory_calls == []


def test_unknown_model_and_foreign_room_are_safe_before_inference(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    unknown = ask(client, model_id="does-not-exist")
    assert unknown.status_code == 400
    assert "does-not-exist" not in unknown.text
    missing = ask(client, room_id="missing")
    foreign = ask(client, room_id=OTHER_ROOM_ID)
    assert (missing.status_code, missing.json()) == (foreign.status_code, foreign.json())
    assert missing.status_code == 404
    assert provider.calls == 0


@pytest.mark.parametrize("mutation", [
    "membership", "identity", "chunk_delete", "chunk_attempt", "chunk_text",
    "job_result", "job_attempt", "job_status", "record_link", "file_link",
])
def test_final_revalidation_blocks_stale_or_unauthorized_answer(rig, mutation):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    def change(session_maker):
        if mutation in {"record_link", "file_link"}:
            # Simulate a damaged linkage despite SQLite's normal FK protection.
            with session_maker.kw["bind"].connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                if mutation == "record_link":
                    connection.exec_driver_sql("UPDATE uploads SET room_id=? WHERE id=?", (OTHER_ROOM_ID, RECORD_ID))
                else:
                    connection.exec_driver_sql("UPDATE upload_files SET upload_id=? WHERE id=?", (OTHER_RECORD_ID, FILE_ID))
                connection.commit()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            return
        with session_maker() as db:
            if mutation == "membership":
                db.query(FamilyMember).filter(FamilyMember.room_id == ROOM_ID, FamilyMember.user_id == USER_ID).update({FamilyMember.is_active: False})
            elif mutation == "identity":
                db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
            elif mutation == "chunk_delete":
                db.query(RecordChunk).filter(RecordChunk.room_id == ROOM_ID).delete()
            elif mutation == "chunk_attempt":
                db.query(RecordChunk).filter(RecordChunk.room_id == ROOM_ID).first().source_attempt = 2
            elif mutation == "chunk_text":
                db.query(RecordChunk).filter(RecordChunk.room_id == ROOM_ID).first().text = "fabricated"
            elif mutation == "job_result":
                db.get(ProcessingJob, JOB_ID).result_json = json.dumps(result_payload("changed"), ensure_ascii=False)
            elif mutation == "job_attempt":
                db.get(ProcessingJob, JOB_ID).attempt = 2
            elif mutation == "job_status":
                db.get(ProcessingJob, JOB_ID).status = "FAILED"
            db.commit()
    provider.mutation = change
    response = ask(client)
    if mutation == "identity":
        assert (response.status_code, response.json()) == (401, {"detail": "Authenticated user not found"})
    elif mutation == "membership":
        assert (response.status_code, response.json()) == (404, {"detail": "Retrieval room not found"})
    else:
        assert (response.status_code, response.json()) == (409, {"detail": "Evidence changed; retry request"})
    assert QUOTE not in response.text and "citations" not in response.text


def test_neighboring_room_source_does_not_affect_answer(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    index(tracker, embedding, record_id=OTHER_RECORD_ID, job_id=OTHER_JOB_ID, user_id=OTHER_USER_ID)
    def change_other(session_maker):
        with session_maker() as db:
            db.get(ProcessingJob, OTHER_JOB_ID).result_json = json.dumps(result_payload("changed"), ensure_ascii=False)
            db.commit()
    provider.mutation = change_other
    response = ask(client)
    assert response.status_code == 200
    assert response.json()["answer"] == "기록에서 추출된 내용\n" + QUOTE


def test_malformed_stored_locator_cannot_be_published_as_citation(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    with tracker.session_maker() as db:
        db.query(RecordChunk).filter(RecordChunk.room_id == ROOM_ID).first().source_locator_json = (
            '{"kind":"full_text","storage_path":"private-path"}'
        )
        db.commit()
    response = ask(client)
    assert response.status_code == 200
    assert response.json()["gate_route"] == "NO_RECORD"
    assert response.json()["citations"] == []
    assert "private-path" not in response.text


def test_indexed_stt_string_timing_cannot_be_published_as_citation(rig):
    tracker, embedding, provider, client = rig
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob, JOB_ID)
        job.processor = "stt"
        job.result_json = json.dumps({
            "text": QUOTE, "extractor": "AUTO_STT", "extractor_id": "stt-v1",
            "segments": [{"text": QUOTE, "start_seconds": "http://private-provider/internal", "end_seconds": 2}],
        }, ensure_ascii=False)
        job.review_json = confirmed_review(json.loads(job.result_json), job.attempt)
        db.commit()
    index(tracker, embedding)
    response = ask(client)
    assert (response.status_code, response.json()) == (409, {"detail": "Evidence changed; retry request"})
    assert "private-provider" not in response.text


@pytest.mark.parametrize("query,text,expected", [
    ("부산 여행의 정확한 날짜는?", QUOTE, "NO_RECORD"),
    ("할머니는 몇 시였어?", QUOTE, "NO_RECORD"),
    ("아버지 검진은 몇 시였어?", None, "CONFLICT"),
    ("아버지 검진은 몇 시였어?", "", "NO_RECORD"),
])
@pytest.mark.parametrize("loss,status", [("membership", 404), ("identity", 401)])
def test_final_acl_recheck_applies_to_every_insufficient_route(rig, query, text, expected, loss, status):
    tracker, embedding, provider, client = rig
    set_result(tracker, QUOTE if text is None else text)
    index(tracker, embedding)
    if text is None:
        add_second_source(tracker)
        index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    # A, search ACL, search load, snapshot, then final read. Change just before final read.
    from app.api.routes import grounded_answering as route
    original = route.SessionLocal
    opens = 0
    def before_final():
        nonlocal opens
        opens += 1
        if opens == 5:
            with tracker.session_maker() as db:
                if loss == "membership":
                    db.query(FamilyMember).filter(FamilyMember.room_id == ROOM_ID, FamilyMember.user_id == USER_ID).update({FamilyMember.is_active: False})
                else:
                    db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
                db.commit()
        return original()
    route.SessionLocal = before_final
    response = ask(client, query, k=2)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": "Retrieval room not found" if status == 404 else "Authenticated user not found"}
    assert provider.calls == 0
    assert provider.factory_calls == []
    assert QUOTE not in response.text
