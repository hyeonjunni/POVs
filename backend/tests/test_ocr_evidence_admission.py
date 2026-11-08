"""OCR review is mandatory at admission and when old evidence is consumed."""
import json
import pytest
from app.models.processing_job import ProcessingJob
from app.models.family import FamilyMember
from app.services.processing_jobs import (
    processing_result_is_evidence, serialize_processing_job, review_ocr_result,
    ProcessingJobNotFound,
)
from app.services.retrieval_indexing import RetrievalIndexNotReady
from ocr_approval_helpers import confirmed_review
import test_retrieval_indexing as ri


@pytest.mark.parametrize("state", ["absent", "unreviewed", "rejected", "attempt", "result", "both", "flags_only"])
def test_ocr_requires_current_explicit_review_before_embedding(state):
    engine, tracker = ri.setup_indexing()
    try:
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, ri.JOB_ID)
            result = json.loads(job.result_json)
            review = json.loads(confirmed_review(result, job.attempt))
            if state in ("absent", "flags_only"):
                job.review_json = None
                if state == "flags_only":
                    result.update(user_confirmed=True, review_status="confirmed")
                    job.result_json = json.dumps(result)
            else:
                if state in ("unreviewed", "rejected"):
                    review["status"] = state
                if state in ("attempt", "both"):
                    review["source_attempt"] += 1
                if state in ("result", "both"):
                    review["source_result_sha256"] = "0" * 64
                job.review_json = json.dumps(review)
            db.commit()
            assert not processing_result_is_evidence(job)
            if state == "absent":
                assert serialize_processing_job(job)["result"] == result  # still reviewable
        provider = ri.FakeEmbeddingProvider(tracker)
        with pytest.raises(RetrievalIndexNotReady):
            ri.index(tracker, provider)
        assert provider.open_count_at_embed == []
        assert ri.persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()


def test_only_uploader_can_authorize_then_chunks_and_embeddings_are_created():
    engine, tracker = ri.setup_indexing()
    try:
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, ri.JOB_ID)
            job.review_json = None
            db.add(FamilyMember(id="other-member", room_id=ri.ROOM_ID, user_id=ri.OTHER_USER_ID, role="member"))
            db.commit()
            with pytest.raises(ProcessingJobNotFound):
                review_ocr_result(db, ri.JOB_ID, ri.OTHER_USER_ID, "approve")
            assert job.review_json is None
        provider = ri.FakeEmbeddingProvider(tracker)
        with pytest.raises(RetrievalIndexNotReady):
            ri.index_processing_result(record_id=ri.RECORD_ID, processing_job_id=ri.JOB_ID,
                user_id=ri.OTHER_USER_ID, provider=provider, session_factory=tracker)
        assert provider.open_count_at_embed == []
        with tracker.session_maker() as db:
            approved = review_ocr_result(db, ri.JOB_ID, ri.USER_ID, "approve")
            assert approved["user_confirmed"] is True
        outcome = ri.index(tracker, provider)
        assert outcome.chunk_count == outcome.embedding_count == 1
        assert all(ri.persisted_rows(tracker))
        assert all(ri.searchable_ids(tracker).values())
    finally:
        engine.dispose()


def test_prior_index_without_review_is_excluded_from_every_search_arm():
    engine, tracker = ri.setup_indexing()
    try:
        ri.index(tracker, ri.FakeEmbeddingProvider(tracker))
        with tracker.session_maker() as db:
            db.get(ProcessingJob, ri.JOB_ID).review_json = None
            db.commit()
        assert not any(ri.searchable_ids(tracker).values())
    finally:
        engine.dispose()


def test_rejected_review_stays_ineligible_after_retry_result():
    engine, tracker = ri.setup_indexing()
    try:
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, ri.JOB_ID)
            job.review_json = None
            db.commit()
            review_ocr_result(db, ri.JOB_ID, ri.USER_ID, "reject")
            job.attempt += 1
            job.result_json = json.dumps(ri.result_payload("new extraction after retry"))
            db.commit()
            assert not processing_result_is_evidence(job)
        with pytest.raises(RetrievalIndexNotReady):
            ri.index(tracker, ri.FakeEmbeddingProvider(tracker))
        assert ri.persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()


def test_stt_without_review_is_ineligible_under_approved_m4_policy():
    from types import SimpleNamespace
    job = SimpleNamespace(status="SUCCEEDED", processor="stt", review_json=None,
                          attempt=1, result_json=json.dumps({"text": "speech", "extractor": "AUTO_STT"}))
    assert not processing_result_is_evidence(job)


def test_approval_removed_during_embedding_cannot_publish(tmp_path):
    from app.services.retrieval_indexing import RetrievalIndexStale
    engine, tracker = ri.setup_file_indexing(tmp_path)
    try:
        def remove_review(sessions):
            with sessions() as db:
                db.get(ProcessingJob, ri.JOB_ID).review_json = None
                db.commit()
        with pytest.raises(RetrievalIndexStale):
            ri.index(tracker, ri.FakeEmbeddingProvider(tracker, mutation=remove_review))
        assert ri.persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()
