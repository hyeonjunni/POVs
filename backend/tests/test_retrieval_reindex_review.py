"""Rejected OCR must stay absent even when an indexing worker races review."""
import pytest

import test_retrieval_indexing as ri
from app.models.processing_job import ProcessingJob
from app.models.upload import UploadFile
from app.services.processing_jobs import review_ocr_result
from app.services.retrieval_indexing import RetrievalIndexNotReady, RetrievalIndexStale


def reject(session_maker):
    with session_maker() as db:
        db.get(UploadFile, ri.FILE_ID).file_uploader_user_id = ri.USER_ID
        db.commit()
        db.get(ProcessingJob, ri.JOB_ID).review_json = None  # historical pre-fix index
        db.commit()
        review_ocr_result(db, ri.JOB_ID, ri.USER_ID, "reject")


def test_rejected_ocr_reindex_does_not_embed_or_recreate_rows():
    engine, tracker = ri.setup_indexing()
    try:
        ri.index(tracker, ri.FakeEmbeddingProvider(tracker))
        assert all(ri.persisted_rows(tracker))
        reject(tracker.session_maker)
        assert ri.persisted_rows(tracker) == ([], [])
        provider = ri.FakeEmbeddingProvider(tracker)
        with pytest.raises(RetrievalIndexNotReady):
            ri.index(tracker, provider)
        assert provider.open_count_at_embed == []
        assert ri.persisted_rows(tracker) == ([], [])
        assert not any(ri.searchable_ids(tracker).values())
    finally:
        engine.dispose()


def test_rejection_during_embedding_cannot_publish_rows(tmp_path):
    engine, tracker = ri.setup_file_indexing(tmp_path)
    try:
        ri.index(tracker, ri.FakeEmbeddingProvider(tracker))
        provider = ri.FakeEmbeddingProvider(tracker, mutation=reject)
        with pytest.raises(RetrievalIndexStale):
            ri.index(tracker, provider)
        assert provider.open_count_at_embed == [0]
        assert ri.persisted_rows(tracker) == ([], [])
        assert not any(ri.searchable_ids(tracker).values())
    finally:
        engine.dispose()


@pytest.mark.parametrize("review", [
    "{}", "[]", "invalid", '{"status":"unknown"}',
    '{"status":"confirmed","source_attempt":999,"source_result_sha256":"stale"}',
])
def test_ineligible_review_reindex_fails_before_embedding(review):
    engine, tracker = ri.setup_indexing()
    try:
        with tracker.session_maker() as db:
            db.get(ProcessingJob, ri.JOB_ID).review_json = review
            db.commit()
        provider = ri.FakeEmbeddingProvider(tracker)
        with pytest.raises(RetrievalIndexNotReady):
            ri.index(tracker, provider)
        assert provider.open_count_at_embed == []
        assert ri.persisted_rows(tracker) == ([], [])
    finally:
        engine.dispose()