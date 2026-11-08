"""Explicit source-bound approvals for synthetic retrieval fixtures."""
import json
from app.retrieval.chunking import canonical_json, sha256_text


def confirmed_review(result, attempt=1):
    return canonical_json(dict(status="confirmed", source_attempt=attempt,
                               source_result_sha256=sha256_text(canonical_json(result))))


def approve_job_fixture(job):
    job.review_json = confirmed_review(json.loads(job.result_json), job.attempt)
