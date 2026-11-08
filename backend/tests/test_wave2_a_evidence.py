import pytest
from test_retrieval_indexing import setup_indexing, index, FakeEmbeddingProvider, searchable_ids, JOB_ID, FILE_ID, USER_ID, ROOM_ID
from app.models.processing_job import ProcessingJob
from app.models.upload import UploadFile
from app.services.processing_jobs import review_ocr_result, serialize_processing_job


def test_rejected_ocr_is_not_public_evidence():
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        assert all(searchable_ids(tracker).values())
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).review_json = None  # historical pre-fix index
            db.get(UploadFile, FILE_ID).file_uploader_user_id = USER_ID
            db.commit()
            review_ocr_result(db, JOB_ID, USER_ID, "reject")
            assert serialize_processing_job(db.get(ProcessingJob, JOB_ID))["result"] is None
        assert not any(searchable_ids(tracker).values())
    finally:
        engine.dispose()

@pytest.mark.parametrize("change", ["failed", "mutated", "attempt", "review", "reindexed_rejection"])
def test_stale_results_are_excluded_from_every_search_arm(change):
    import json
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, JOB_ID)
            if change == "failed":
                job.status = "FAILED"
            elif change == "mutated":
                result = json.loads(job.result_json)
                result["extractor_id"] = "changed"
                job.result_json = json.dumps(result)
            elif change == "attempt":
                job.attempt += 1
            elif change == "review":
                job.review_json = "{}"
            else:
                db.get(ProcessingJob, JOB_ID).review_json = None  # historical pre-fix index
                db.get(UploadFile, FILE_ID).file_uploader_user_id = USER_ID
                db.commit()
                review_ocr_result(db, JOB_ID, USER_ID, "reject")
            db.commit()
        if change == "reindexed_rejection":
            from app.services.retrieval_indexing import RetrievalIndexNotReady
            with pytest.raises(RetrievalIndexNotReady):
                index(tracker, FakeEmbeddingProvider(tracker))
        assert not any(searchable_ids(tracker).values())
    finally:
        engine.dispose()


from test_grounded_answering_api import rig, set_result, add_second_source, ask
from test_grounded_answering_api import index as grounded_index


@pytest.mark.parametrize("k", [1, 2])
def test_conflict_detection_independent_of_display_k(rig, k):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    add_second_source(tracker)
    grounded_index(tracker, embedding)
    grounded_index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    response = ask(client, k=k)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "CONFLICT"
    assert len(response.json()["citations"]) <= k
    assert provider.calls == 0


def test_rejection_invalidates_old_facts_and_ask(rig):
    from sqlalchemy import select
    from app.models.retrieval import RecordChunk, RecordEmbedding
    from app.retrieval.chunking import RetrievalChunk
    from app.ai.grounded_answering import _source_is_current, build_fact_sheet
    tracker, embedding, provider, client = rig
    set_result(tracker)
    grounded_index(tracker, embedding)
    with tracker.session_maker() as db:
        row = db.scalar(select(RecordChunk))
        fact = build_fact_sheet([RetrievalChunk(**{key: getattr(row, key)
            for key in RetrievalChunk.__dataclass_fields__})]).facts[0]
        assert _source_is_current(db, fact, ROOM_ID)
        db.get(ProcessingJob, JOB_ID).review_json = None  # historical pre-fix index
        db.get(UploadFile, FILE_ID).file_uploader_user_id = USER_ID
        db.commit()
        review_ocr_result(db, JOB_ID, USER_ID, "reject")
        assert db.scalar(select(RecordChunk)) is None
        assert db.scalar(select(RecordEmbedding)) is None
        assert not _source_is_current(db, fact, ROOM_ID)
    response = ask(client)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "NO_RECORD"
    assert response.json()["citations"] == []
    assert provider.calls == 0


@pytest.mark.parametrize("review", ["{}", "[]", "broken", '{"status":"rejected"}'])
def test_malformed_review_cannot_supply_public_result(review):
    from app.services.processing_jobs import processing_result_is_evidence
    engine, tracker = setup_indexing()
    try:
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, JOB_ID)
            job.review_json = review
            db.commit()
            assert not processing_result_is_evidence(job)
            assert serialize_processing_job(job)["result"] is None
    finally:
        engine.dispose()


def test_reject_invalidation_rolls_back_with_failed_commit(monkeypatch):
    from sqlalchemy import select
    from app.models.retrieval import RecordChunk
    engine, tracker = setup_indexing()
    try:
        index(tracker, FakeEmbeddingProvider(tracker))
        with tracker.session_maker() as db:
            db.get(ProcessingJob, JOB_ID).review_json = None  # historical pre-fix index
            db.get(UploadFile, FILE_ID).file_uploader_user_id = USER_ID
            db.commit()
            def fail():
                raise RuntimeError("synthetic commit failure")
            monkeypatch.setattr(db, "commit", fail)
            with pytest.raises(RuntimeError):
                review_ocr_result(db, JOB_ID, USER_ID, "reject")
            db.rollback()
            assert db.get(ProcessingJob, JOB_ID).review_json is None
            assert db.scalar(select(RecordChunk)) is not None
        assert not any(searchable_ids(tracker).values())
    finally:
        engine.dispose()


from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import media_client
from test_secure_retrieval import processed, index_secure, ask_secure
from app.retrieval.evaluation import SyntheticTokenHashProvider


@pytest.mark.parametrize("k", [1, 2])
def test_secure_conflict_detection_independent_of_display_k(media_client, monkeypatch, k):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    for quote in ("아버지 검진은 오후 2시였다.", "아버지 검진은 오전 9시였다."):
        row, done = processed(c, monkeypatch, quote)
        response = index_secure(c, row, done)
        assert response.status_code == 200, response.text
    response = ask_secure(c, k=k)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "CONFLICT"
    assert len(response.json()["citations"]) <= k


def test_entire_safety_pool_is_revalidated_after_generation(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    add_second_source(tracker, "아버지 검진은 오후 2시였다.")
    grounded_index(tracker, embedding)
    grounded_index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    provider.mutation = lambda _: set_result(tracker, "아버지 검진은 오전 9시였다.", job_id="job-ask-2")
    response = ask(client, k=1)
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("k", [1, 2])
def test_safe_answer_citations_respect_display_k(rig, k):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    add_second_source(tracker, "아버지 검진은 오후 2시였다.")
    grounded_index(tracker, embedding)
    grounded_index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    provider.error = RuntimeError("synthetic fallback to all planned claims")
    response = ask(client, k=k)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "ANSWER"
    assert len(response.json()["citations"]) == k


@pytest.mark.parametrize("k", [1, 2])
def test_specific_person_question_is_not_ambiguous_due_to_other_person(rig, k):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    add_second_source(tracker, "어머니 산책은 오후 3시였다.")
    grounded_index(tracker, embedding)
    grounded_index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    response = ask(client, k=k)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "ANSWER"
    assert "아버지 검진은 오후 2시였다." in response.json()["answer"]
    assert "어머니" not in response.json()["answer"]
    assert all(c["record_id"] != "record-ask-2" for c in response.json()["citations"])


@pytest.mark.parametrize("k", [1, 2])
def test_secure_specific_person_answer_ignores_other_person(media_client, monkeypatch, k):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    for quote in ("아버지 검진은 오후 2시였다.", "어머니 산책은 오후 3시였다."):
        row, done = processed(c, monkeypatch, quote)
        assert index_secure(c, row, done).status_code == 200
    response = ask_secure(c, k=k)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "ANSWER"
    assert "아버지 검진은 오후 2시였다." in response.json()["answer"]
    assert "어머니" not in response.json()["answer"]
    assert all(c["record_id"] != row["upload_id"] for c in response.json()["citations"])


@pytest.mark.parametrize("evidence,expected", [
    ("어머니 산책은 오후 3시였다.", False),
    ("검진은 오후 2시였다.", True),
    ("아버지와 어머니 검진은 오후 2시였다.", True),
])
def test_person_scope_keeps_unknown_and_shared_references(evidence, expected):
    from app.ai.gates.answer_router import evidence_matches_query_person
    from app.ai.gates.entity_index import RoomEntityIndex
    assert evidence_matches_query_person("아버지 검진 몇 시?", evidence, RoomEntityIndex()) is expected


@pytest.mark.parametrize("k", [1, 2])
def test_other_person_time_cannot_satisfy_requested_person_answerability(rig, k):
    tracker, embedding, provider, client = rig
    set_result(tracker, "아버지 검진 기록이다.")
    add_second_source(tracker, "어머니 검진은 오후 3시였다.")
    grounded_index(tracker, embedding)
    grounded_index(tracker, embedding, record_id="record-ask-2", job_id="job-ask-2")
    response = ask(client, k=k)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] in ("REFUSE", "NO_RECORD")
    assert response.json()["citations"] == []
    assert provider.calls == 0


@pytest.mark.parametrize("k", [1, 2])
def test_secure_other_person_time_cannot_satisfy_answerability(media_client, monkeypatch, k):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    for quote in ("아버지 검진 기록이다.", "어머니 검진은 오후 3시였다."):
        row, done = processed(c, monkeypatch, quote)
        assert index_secure(c, row, done).status_code == 200
    response = ask_secure(c, k=k)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] in ("REFUSE", "NO_RECORD")
    assert response.json()["citations"] == []
    assert response.json()["provider_mode"] == "not_called"
    assert response.json()["gate_action"] == "llm_not_called"


@pytest.mark.parametrize("evidence", [
    "검진은 오후 2시였다.",
    "아버지와 어머니 검진은 오후 2시였다.",
    "아버지 검진은 오후 2시였다.",
])
def test_answerability_retains_unspecified_shared_and_matching_person(evidence):
    from app.ai.gates.answer_router import route_query
    decision = route_query("아버지 검진은 몇 시였어?", [evidence])
    assert decision.route == "ANSWER"
