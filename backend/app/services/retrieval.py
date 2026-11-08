from __future__ import annotations

from app.core.demo_trace import observe
import json
from collections.abc import Callable
from dataclasses import fields
from time import perf_counter_ns
from typing import Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import select, type_coerce
from sqlalchemy.orm import Session

from app.models.auth_identity import require_persisted_principal
from app.models.family import FamilyMember
from app.models.processing_job import ProcessingJob
from app.models.retrieval import M2_EMBEDDING_DIMENSIONS, RecordChunk, RecordEmbedding
from app.models.upload import Upload, UploadFile
from app.retrieval.chunking import ChunkSource, RetrievalChunk, canonical_json, chunk_processing_result, sha256_text
from app.retrieval.embeddings import EmbeddingProviderError, validate_embeddings
from app.retrieval.ranking import RankedChunk, fuse_rrf, rank_bm25, rank_substring
from app.retrieval.vector import rank_vector
from app.services.processing_jobs import processing_result_is_evidence
from app.services.record_policy import allowed_record_ids

LexicalArm = Literal["substring", "bm25"]
_CHUNK_FIELDS = tuple(field.name for field in fields(RetrievalChunk))


class RetrievalSearchNotFound(LookupError):
    pass


class RetrievalSearchInvalid(ValueError):
    pass


def _require_room_member(db: Session, room_id: str, user_id: str) -> None:
    require_persisted_principal(db, user_id)
    if db.scalar(select(FamilyMember.id).where(FamilyMember.room_id == room_id, FamilyMember.user_id == user_id, FamilyMember.is_active.is_(True)).limit(1)) is None:
        raise RetrievalSearchNotFound("Retrieval room not found")


def chunk_is_current(db: Session, chunk: RetrievalChunk, room_id: str) -> bool:
    """Only the latest, unchanged successful source may supply public evidence."""
    job = db.get(ProcessingJob, chunk.processing_job_id)
    record = db.get(Upload, chunk.record_id)
    upload_file = db.get(UploadFile, chunk.file_id)
    if (chunk.room_id != room_id or job is None or record is None or upload_file is None
            or not processing_result_is_evidence(job)
            or job.room_id != room_id or job.record_id != chunk.record_id
            or job.upload_file_id != chunk.file_id or job.attempt != chunk.source_attempt
            or record.room_id != room_id or upload_file.upload_id != chunk.record_id):
        return False
    latest = db.scalar(select(UploadFile.id).where(UploadFile.upload_id == chunk.record_id)
                       .order_by(UploadFile.created_at.desc(), UploadFile.id.desc()).limit(1))
    if latest != chunk.file_id:
        return False
    try:
        result = json.loads(job.result_json)
        return (sha256_text(canonical_json(result)) == chunk.source_result_sha256
                and sha256_text(chunk.text) == chunk.text_sha256
                and chunk in chunk_processing_result(ChunkSource(
                    room_id=room_id, record_id=chunk.record_id, file_id=chunk.file_id,
                    processing_job_id=chunk.processing_job_id,
                    source_attempt=chunk.source_attempt, result=result,
                    chunker_version=chunk.chunker_version,
                )))
    except (TypeError, ValueError, KeyError):
        return False


def vector_statement(*, room_id: str, model_key: str, query_vector: list[float], k: int):
    distance = type_coerce(
        RecordEmbedding.embedding, Vector(M2_EMBEDDING_DIMENSIONS)
    ).cosine_distance(query_vector).label("distance")
    return (
        select(*(getattr(RecordChunk, field) for field in _CHUNK_FIELDS), distance)
        .join(RecordEmbedding, RecordEmbedding.chunk_id == RecordChunk.id)
        .where(
            RecordChunk.room_id == room_id,
            RecordEmbedding.model_key == model_key,
            RecordEmbedding.dimensions == M2_EMBEDDING_DIMENSIONS,
        )
        .order_by(distance, RecordChunk.id)
        .limit(k)
    )


def search_room_lexical(
    *,
    room_id: str,
    query: str,
    arm: LexicalArm,
    k: int,
    session_factory: Callable[[], Session],
) -> list[RankedChunk]:
    db = session_factory()
    try:
        # ponytail: bounded room-local O(N) scan; replace only after measured corpus/latency exceeds the approved ceiling.
        rows = db.execute(
            select(*(getattr(RecordChunk, field) for field in _CHUNK_FIELDS)).where(
                RecordChunk.room_id == room_id
            )
        ).mappings()
        chunks = tuple(chunk for row in rows
                       if chunk_is_current(db, chunk := RetrievalChunk(**dict(row)), room_id))
    finally:
        db.close()

    if arm == "substring":
        return rank_substring(chunks, query)[:k]
    if arm == "bm25":
        return rank_bm25(chunks, query)[:k]
    raise ValueError("Unsupported lexical retrieval arm")


def search_room(
    *, room_id: str, user_id: str, query: str, arm: str, k: int, provider,
    session_factory: Callable[[], Session], candidate_limit: int,
    timings: dict[str, int] | None = None,
    safety_candidates: list[RetrievalChunk] | None = None,
) -> list[RankedChunk]:
    stages = {
        "query_embedding_ns": 0, "db_candidate_load_ns": 0, "bm25_ns": 0,
        "vector_ns": 0, "fusion_ns": 0, "end_to_end_ns": 0,
    }
    started = perf_counter_ns()
    try:
        if not query.strip():
            raise RetrievalSearchInvalid("Query must not be empty")
        if arm == "hybrid_rrf" and candidate_limit < 1:
            raise RetrievalSearchInvalid("Hybrid candidate limit must be positive")
        db_started = perf_counter_ns()
        db = session_factory()
        try:
            _require_room_member(db, room_id, user_id)
            authorized_ids = allowed_record_ids(db, user_id, room_id, purpose="rag")
        finally:
            db.close()
            stages["db_candidate_load_ns"] += perf_counter_ns() - db_started

        if arm in {"vector", "hybrid_rrf"}:
            if getattr(provider, "dimensions", None) != M2_EMBEDDING_DIMENSIONS or not str(getattr(provider, "model_key", "")).strip():
                raise RetrievalSearchInvalid("Vector search is not configured")
            embedding_started = perf_counter_ns()
            try:
                query_vector = validate_embeddings(
                    _demo_embed(provider, query), expected_count=1,
                    dimensions=M2_EMBEDDING_DIMENSIONS,
                )[0]
            except EmbeddingProviderError:
                raise
            except Exception:
                raise EmbeddingProviderError("Embedding provider failed") from None
            finally:
                stages["query_embedding_ns"] = perf_counter_ns() - embedding_started
        else:
            query_vector = None

        db_started = perf_counter_ns()
        db = session_factory()
        try:
            _require_room_member(db, room_id, user_id)
            authorized_ids = allowed_record_ids(db, user_id, room_id, purpose="rag")
            statement = select(*(getattr(RecordChunk, field) for field in _CHUNK_FIELDS)).where(
                RecordChunk.room_id == room_id,
                RecordChunk.record_id.in_(authorized_ids),
            )
            chunks = tuple(RetrievalChunk(**dict(row)) for row in db.execute(statement).mappings())
            chunks = tuple(chunk for chunk in chunks if chunk_is_current(db, chunk, room_id))
            if safety_candidates is not None:
                safety_candidates.extend(chunks)
            # Answering admits relevance before bounding evidence. Public search
            # retains its existing display/candidate depths.
            result_k = len(chunks) if safety_candidates is not None else k
            current_ids = {chunk.id for chunk in chunks}
            if arm not in {"substring", "bm25"}:
                model_key = provider.model_key
                candidate_k = (
                    min(max(k * 4, 20), candidate_limit)
                    if arm == "hybrid_rrf" else k
                )
                eligible_ids = set()
                if arm == "hybrid_rrf":
                    eligible_ids = set(db.scalars(
                        select(RecordEmbedding.chunk_id)
                        .join(RecordChunk, RecordEmbedding.chunk_id == RecordChunk.id)
                        .where(
                            RecordChunk.room_id == room_id,
                            RecordChunk.record_id.in_(authorized_ids),
                            RecordEmbedding.model_key == model_key,
                            RecordEmbedding.dimensions == M2_EMBEDDING_DIMENSIONS,
                        )
                    ))
                if safety_candidates is not None:
                    candidate_k = max(1, len(chunks))
                if db.bind.dialect.name == "postgresql":
                    vector_rows = [
                        (
                            RetrievalChunk(**{field: row[field] for field in _CHUNK_FIELDS}),
                            1 - float(row["distance"]),
                        )
                        for row in db.execute(vector_statement(
                            room_id=room_id, model_key=model_key,
                            query_vector=query_vector, k=candidate_k,
                        ).where(RecordChunk.id.in_(current_ids))).mappings()
                    ]
                else:
                    vector_rows = [
                        (RetrievalChunk(**{field: row[field] for field in _CHUNK_FIELDS}), row["embedding"])
                        for row in db.execute(
                            select(
                                *(getattr(RecordChunk, field) for field in _CHUNK_FIELDS),
                                RecordEmbedding.embedding,
                            )
                            .join(RecordEmbedding, RecordEmbedding.chunk_id == RecordChunk.id)
                            .where(
                                RecordChunk.room_id == room_id,
                                RecordChunk.record_id.in_(authorized_ids),
                                RecordEmbedding.model_key == model_key,
                                RecordEmbedding.dimensions == M2_EMBEDDING_DIMENSIONS,
                            )
                        ).mappings()
                    ]
                vector_rows = [(chunk, score) for chunk, score in vector_rows
                               if chunk.id in current_ids and chunk_is_current(db, chunk, room_id)]
        finally:
            db.close()
            stages["db_candidate_load_ns"] += perf_counter_ns() - db_started

        if arm == "substring":
            return rank_substring(chunks, query)[:result_k]
        if arm == "bm25":
            bm25_started = perf_counter_ns()
            try:
                return rank_bm25(chunks, query)[:result_k]
            finally:
                stages["bm25_ns"] = perf_counter_ns() - bm25_started
        vector_started = perf_counter_ns()
        vector = (
            vector_rows if vector_rows and isinstance(vector_rows[0][1], float)
            else [(item.chunk, item.score) for item in rank_vector(vector_rows, query_vector)]
        )
        stages["vector_ns"] = perf_counter_ns() - vector_started
        if arm == "vector":
            return [RankedChunk(chunk=chunk, score=score, cosine=score, arms=frozenset({"vector"})) for chunk, score in vector][:result_k]
        if arm == "hybrid_rrf":
            bm25_started = perf_counter_ns()
            bm25 = rank_bm25([chunk for chunk in chunks if chunk.id in eligible_ids], query)[:candidate_k]
            stages["bm25_ns"] = perf_counter_ns() - bm25_started
            fusion_started = perf_counter_ns()
            try:
                return fuse_rrf([(item.chunk, item.score) for item in bm25], vector[:candidate_k])[:result_k]
            finally:
                stages["fusion_ns"] = perf_counter_ns() - fusion_started
        raise RetrievalSearchInvalid("Unsupported retrieval arm")
    finally:
        stages["end_to_end_ns"] = perf_counter_ns() - started
        if timings is not None:
            timings.update(stages)


def search_secure_room(*, runtime, user_id, room_id, query, arm, k, candidate_limit, timings=None, for_answering=False):
    from app.services.secure_retrieval import materialize_room, synthetic_embedding, revalidate_evidence
    started = perf_counter_ns()
    # Public retrieval remains capped at 50; grounded answering privately requests one sentinel.
    if not query.strip() or len(query) > 4096 or arm not in {"substring", "bm25", "vector", "hybrid_rrf"} or not 1 <= k <= 51 or candidate_limit < 1:
        raise RetrievalSearchInvalid("Invalid search request")
    corpus = materialize_room(runtime, user_id, room_id, timings=timings)
    provider = None
    if arm in {"vector", "hybrid_rrf"}:
        provider = synthetic_embedding(getattr(runtime, "embedding_provider", None), runtime=runtime)
        embed_start = perf_counter_ns()
        query_vector = validate_embeddings(_demo_embed(provider, query), expected_count=1, dimensions=M2_EMBEDDING_DIMENSIONS)[0]
        if timings is not None:
            timings["query_embedding_ns"] = perf_counter_ns() - embed_start
    result_k = len(corpus.chunks) if for_answering else k
    rank_start = perf_counter_ns()
    if arm == "substring":
        hits = rank_substring(corpus.chunks, query)[:result_k]
    elif arm == "bm25":
        hits = rank_bm25(corpus.chunks, query)[:result_k]
    else:
        rows = corpus.vectors if corpus.model_key == provider.model_key else ()
        candidate_k = min(max(k * 4, 20), candidate_limit) if arm == "hybrid_rrf" else k
        if for_answering:
            candidate_k = len(corpus.chunks)
        vector = rank_vector(rows, query_vector)[:candidate_k]
        if arm == "vector":
            hits = vector[:result_k]
        else:
            eligible = {c.id for c, _ in rows}
            bm25 = rank_bm25([c for c in corpus.chunks if c.id in eligible], query)[:candidate_k]
            hits = fuse_rrf([(h.chunk, h.score) for h in bm25], [(h.chunk, h.score) for h in vector])[:result_k]
    if timings is not None:
        timings["ranking_ns"] = perf_counter_ns() - rank_start
    revalidate_evidence(runtime, corpus)
    if timings is not None:
        timings["end_to_end_ns"] = perf_counter_ns() - started
    return hits, corpus


@observe("QUERY EMBEDDING", lambda result, a, kw: dict(
    model=a[0].model_key if a[0].model_key == "bge-m3:latest" else "other",
    dimension=a[0].dimensions))
def _demo_embed(provider, query):
    return provider.embed([query])
