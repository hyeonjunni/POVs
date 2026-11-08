from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from time import perf_counter_ns

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.auth_identity import AuthIdentity
from app.models.family import FamilyMember, FamilyRoom
from app.models.processing_job import ProcessingJob
from app.models.retrieval import M2_EMBEDDING_DIMENSIONS
from app.models.retrieval import RecordChunk
from app.models.upload import Upload, UploadFile
from app.models.user import User
from app.services.retrieval import search_room
from app.services.retrieval_indexing import index_processing_result

METRIC_CUTOFFS = (1, 3, 5, 10)
ARMS = ("substring", "bm25", "vector", "hybrid_rrf")
TIMING_STAGES = ("query_embedding_ns", "db_candidate_load_ns", "bm25_ns", "vector_ns", "fusion_ns", "end_to_end_ns")


def _record_ids(ranked_record_ids: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(ranked_record_ids))


def query_metrics(ranked_record_ids: Sequence[str], relevant_record_ids: set[str], k: int) -> dict[str, float] | None:
    if not relevant_record_ids:
        return None
    hits = [rank for rank, record_id in enumerate(_record_ids(ranked_record_ids)[:k], 1) if record_id in relevant_record_ids]
    dcg = sum(1 / math.log2(rank + 1) for rank in hits)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(relevant_record_ids)) + 1))
    return {"recall": len(hits) / len(relevant_record_ids), "mrr": 1 / hits[0] if hits else 0.0, "ndcg": dcg / ideal if ideal else 0.0}


def evaluate_rankings(rankings: Mapping[str, Mapping[str, Sequence[str]]], *, cutoffs: Sequence[int] = METRIC_CUTOFFS) -> dict[str, object]:
    judged = [item for item in rankings.values() if item["relevant_record_ids"]]
    return {
        "query_count": len(rankings), "judged_query_count": len(judged), "no_qrel_query_count": len(rankings) - len(judged),
        "empty_result_query_count": sum(not item["ranked_record_ids"] for item in rankings.values()),
        "macro": {
            str(k): {
                metric: sum(query_metrics(item["ranked_record_ids"], set(item["relevant_record_ids"]), k)[metric] for item in judged) / len(judged) if judged else 0.0
                for metric in ("recall", "mrr", "ndcg")
            }
            for k in cutoffs
        },
    }


def nearest_rank_percentiles(samples: Sequence[int]) -> dict[str, int]:
    ordered = sorted(samples)
    return {"sample_count": len(ordered), "p50_ns": ordered[math.ceil(len(ordered) * .5) - 1] if ordered else 0, "p95_ns": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else 0}


class SyntheticTokenHashProvider:
    """Deterministic control-flow provider; its timing is synthetic, not model evidence."""

    model_key = "synthetic-token-hash@v1"
    dimensions = M2_EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in re.findall(r"[^\W_]+", text.casefold(), re.UNICODE) or [text]:
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:2], "big") % self.dimensions] += 1.0 if digest[2] & 1 else -1.0
            vectors.append(vector)
        return vectors


def _fixture_hash(fixture: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _seed(fixture: Mapping[str, object], sessions) -> list[tuple[str, str, str]]:
    indexed: list[tuple[str, str, str]] = []
    with sessions() as db:
        for room in fixture["rooms"]:
            room_id, user_id = room["id"], room["user_id"]
            db.add(User(id=user_id, username=user_id, email=f"{user_id}@eval.test", name=user_id))
            db.flush()
            db.add_all((
                AuthIdentity(
                    id=f"identity-{user_id}", user_id=user_id, provider="local",
                    provider_subject=user_id, credential_hash="synthetic",
                ),
                FamilyRoom(
                    id=room_id, name=room_id, invite_code=f"code-{room_id}",
                    owner_user_id=user_id,
                ),
            ))
            db.flush()
            db.add(FamilyMember(id=f"member-{room_id}", room_id=room_id, user_id=user_id, role="owner"))
            for record in room["records"]:
                record_id = record["id"]
                file_id, job_id = f"file-{record_id}", f"job-{record_id}"
                db.add(Upload(
                    id=record_id, room_id=room_id, uploader_user_id=user_id,
                    type="text", title=record["title"], status="uploaded",
                ))
                db.flush()
                from app.services.record_policy import register_legacy_record
                register_legacy_record(db, record_id)
                db.add(UploadFile(
                    id=file_id, upload_id=record_id, storage_bucket="synthetic",
                    storage_path=f"{record_id}.txt", mime_type="text/plain",
                    file_uploader_user_id=user_id,
                ))
                db.flush()
                db.add(ProcessingJob(
                    id=job_id, record_id=record_id, upload_file_id=file_id,
                    room_id=room_id, processor="ocr", status="SUCCEEDED", attempt=1,
                    result_json=json.dumps({
                        "text": record["text"], "extractor": "AUTO_OCR",
                        "extractor_id": "synthetic-eval", "review_status": "unreviewed",
                        "user_confirmed": False,
                    }, ensure_ascii=False),
                ))
                indexed.append((record_id, job_id, user_id))
        db.commit()
    # Synthetic OCR fixtures follow the same explicit uploader review as uploads.
    from app.services.processing_jobs import review_ocr_result
    with sessions() as db:
        for _record_id, job_id, user_id in indexed:
            review_ocr_result(db, job_id, user_id, "approve")
    return indexed


def _split_report(queries: Sequence[Mapping[str, object]], rankings: Mapping[str, Mapping[str, list[str]]]) -> dict[str, object]:
    per_query, inputs = {}, {}
    for query in queries:
        query_id, relevant, ranks = query["id"], list(query["relevant_record_ids"]), rankings[query["id"]]
        per_query[query_id] = {"ranked_record_ids_by_k": ranks, "metrics": {str(k): query_metrics(ranks[str(k)], set(relevant), k) for k in METRIC_CUTOFFS}}
        inputs[query_id] = {"ranked_record_ids_by_k": ranks, "relevant_record_ids": relevant}
    counts = evaluate_rankings({query_id: {"ranked_record_ids": item["ranked_record_ids_by_k"][str(max(METRIC_CUTOFFS))], "relevant_record_ids": item["relevant_record_ids"]} for query_id, item in inputs.items()})
    counts["macro"] = {str(k): evaluate_rankings({query_id: {"ranked_record_ids": item["ranked_record_ids_by_k"][str(k)], "relevant_record_ids": item["relevant_record_ids"]} for query_id, item in inputs.items()}, cutoffs=(k,))["macro"][str(k)] for k in METRIC_CUTOFFS}
    return {"per_query": per_query, "summary": counts}


def _chunk_count(sessions) -> int:
    with sessions() as db:
        return db.scalar(select(func.count()).select_from(RecordChunk))


def run_fixture_evaluation(fixture: Mapping[str, object], *, warmups: int = 5, iterations: int = 30, fixture_sha256: str | None = None) -> dict[str, object]:
    if warmups < 0 or iterations < 1:
        raise ValueError("warmups must be nonnegative and iterations must be positive")
    engine, sessions, provider = *_session_factory(), SyntheticTokenHashProvider()
    try:
        index_samples, index_responses = [], []
        for record_id, job_id, user_id in _seed(fixture, sessions):
            started = perf_counter_ns()
            index_responses.append(index_processing_result(
                record_id=record_id, processing_job_id=job_id, user_id=user_id,
                provider=provider, session_factory=sessions,
            ))
            index_samples.append(perf_counter_ns() - started)
        user_ids, queries, arms, latency = ({room["id"]: room["user_id"] for room in fixture["rooms"]}, list(fixture["queries"]), {}, {})
        for arm in ARMS:
            rankings, samples, by_query_k = {query["id"]: {} for query in queries}, defaultdict(list), {}
            for query in queries:
                for k in METRIC_CUTOFFS:
                    query_samples = defaultdict(list)
                    for repeat in range(warmups + iterations):
                        timings: dict[str, int] = {}
                        results = search_room(room_id=query["room_id"], user_id=user_ids[query["room_id"]], query=query["query"], arm=arm, k=k, provider=provider, session_factory=sessions, candidate_limit=fixture["candidate_limit"], timings=timings)
                        if repeat >= warmups:
                            for stage in TIMING_STAGES:
                                samples[stage].append(timings[stage])
                                query_samples[stage].append(timings[stage])
                            if repeat == warmups:
                                rankings[query["id"]][str(k)] = _record_ids([item.chunk.record_id for item in results])
                    by_query_k.setdefault(query["id"], {})[str(k)] = {stage: nearest_rank_percentiles(query_samples[stage]) for stage in TIMING_STAGES}
            arms[arm] = {split: _split_report([query for query in queries if query["split"] == split], rankings) for split in ("tuning", "evaluation")}
            latency[arm] = {"aggregate": {stage: nearest_rank_percentiles(samples[stage]) for stage in TIMING_STAGES}, "by_query_k": by_query_k}
        return {
            "fixture_sha256": fixture_sha256 or _fixture_hash(fixture),
            "metadata": {"fixture_version": fixture["version"], "corpus_record_count": sum(len(room["records"]) for room in fixture["rooms"]), "corpus_chunk_count": _chunk_count(sessions), "candidate_limit": fixture["candidate_limit"], "chunker_version": index_responses[0].chunker_version, "model_key": provider.model_key, "git_sha": _git_sha(), "evidence_class": "synthetic-control-flow", "provider_time_class": "synthetic", "platform": platform.platform() or "unavailable", "machine": platform.machine() or "unavailable", "processor": platform.processor() or "unavailable", "cold_vs_warm": "warm sequential retrieval after indexing", "concurrency": 1, "warmups": warmups, "iterations": iterations, "evaluation_truncation": "production search runs each k; record IDs are deduplicated before metric cutoff"},
            "indexing": {"index_end_to_end_ns": nearest_rank_percentiles(index_samples)}, "arms": arms, "latency": latency,
        }
    finally:
        engine.dispose()
