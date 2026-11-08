from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.auth_identity import require_persisted_principal
from app.models.processing_job import ProcessingJob
from app.models.retrieval import M2_EMBEDDING_DIMENSIONS, RecordChunk, RecordEmbedding
from app.models.upload import Upload, UploadFile
from app.retrieval.chunking import (
    ChunkSource,
    canonical_json,
    chunk_processing_result,
    sha256_text,
)
from app.retrieval.embeddings import (
    EmbeddingProvider,
    EmbeddingProviderError,
    validate_embeddings,
)
from app.schemas.retrieval import RetrievalIndexResponse
from app.services.processing_jobs import (
    ProcessingJobNotFound,
    get_job_for_member,
    processing_result_is_evidence,
)

_EMBEDDING_NAMESPACE = uuid.UUID("a88b3f41-9c67-40b6-a66d-e46517c04bb7")


class RetrievalIndexNotFound(LookupError):
    pass


class RetrievalIndexNotReady(ValueError):
    pass


class RetrievalIndexStale(RuntimeError):
    pass


@dataclass(frozen=True)
class _SourceSnapshot:
    source: ChunkSource
    result_hash: str

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.source.room_id,
            self.source.record_id,
            self.source.file_id,
            self.source.processing_job_id,
            self.source.source_attempt,
            self.result_hash,
        )


def _load_source(
    db: Session, *, record_id: str, processing_job_id: str, user_id: str
) -> _SourceSnapshot:
    require_persisted_principal(db, user_id)
    from app.services.record_policy import RecordAccessDenied, require_record_access
    try:
        require_record_access(db, user_id, record_id, purpose="rag")
    except RecordAccessDenied as error:
        raise RetrievalIndexNotFound("Retrieval source not found") from error
    try:
        job = get_job_for_member(db, job_id=processing_job_id, user_id=user_id)
    except ProcessingJobNotFound as error:
        raise RetrievalIndexNotFound("Retrieval source not found") from error

    record = db.get(Upload, record_id)
    upload_file = db.get(UploadFile, job.upload_file_id)
    if (
        record is None
        or upload_file is None
        or job.record_id != record_id
        or job.room_id != record.room_id
        or upload_file.id != job.upload_file_id
        or upload_file.upload_id != record.id
    ):
        raise RetrievalIndexNotFound("Retrieval source not found")
    latest = db.scalar(select(UploadFile.id).where(UploadFile.upload_id == record_id)
                       .order_by(UploadFile.created_at.desc(), UploadFile.id.desc()).limit(1))
    if latest != upload_file.id:
        raise RetrievalIndexNotReady("Processing source has been superseded")
    if job.status != "SUCCEEDED":
        raise RetrievalIndexNotReady("Processing job has not succeeded")
    if not processing_result_is_evidence(job):
        raise RetrievalIndexNotReady("Processing result is not eligible evidence")

    try:
        result = json.loads(job.result_json or "null")
    except (TypeError, json.JSONDecodeError) as error:
        raise RetrievalIndexNotReady("Processing job has no valid result") from error
    if not isinstance(result, Mapping):
        raise RetrievalIndexNotReady("Processing job has no valid result")
    expected_extractor = {"ocr": "AUTO_OCR", "stt": "AUTO_STT", "text": "NATIVE_TEXT"}.get(job.processor)
    if (
        not isinstance(result.get("text"), str)
        or result.get("extractor") != expected_extractor
        or not isinstance(result.get("extractor_id"), str)
        or not result["extractor_id"].strip()
    ):
        raise RetrievalIndexNotReady("Processing job has no valid result")

    source = ChunkSource(
        room_id=record.room_id,
        record_id=record.id,
        file_id=upload_file.id,
        processing_job_id=job.id,
        source_attempt=job.attempt,
        result=result,
    )
    return _SourceSnapshot(
        source=source,
        result_hash=sha256_text(source.canonical_result_json),
    )


def _embedding_id(chunk_id: str, model_key: str) -> str:
    return str(uuid.uuid5(_EMBEDDING_NAMESPACE, canonical_json([chunk_id, model_key])))


def serialize_record(db: Session, record_id: str) -> None:
    """Serialize index publication with replacement publication on one record."""
    from app.services.storage_quota import _lock_room, StorageAllocationStateChanged
    room_id = db.scalar(select(Upload.room_id).where(Upload.id == record_id))
    if room_id is None:
        raise RetrievalIndexStale("Retrieval source changed during indexing")
    try:
        _lock_room(db, room_id)
    except StorageAllocationStateChanged as error:
        raise RetrievalIndexStale("Retrieval source changed during indexing") from error
    if db.execute(update(Upload).where(Upload.id == record_id).values(id=Upload.id)).rowcount != 1:
        raise RetrievalIndexStale("Retrieval source changed during indexing")


def _serialize_job(db: Session, processing_job_id: str) -> None:
    table = ProcessingJob.__table__
    rowcount = db.execute(
        update(table)
        .where(table.c.id == processing_job_id)
        .values(id=table.c.id, updated_at=table.c.updated_at)
    ).rowcount
    if rowcount != 1:
        raise RetrievalIndexStale("Retrieval source changed during indexing")


def index_processing_result(
    *,
    record_id: str,
    processing_job_id: str,
    user_id: str,
    provider: EmbeddingProvider,
    session_factory: Callable[[], Session],
) -> RetrievalIndexResponse:
    db = session_factory()
    try:
        snapshot = _load_source(
            db,
            record_id=record_id,
            processing_job_id=processing_job_id,
            user_id=user_id,
        )
    finally:
        db.close()

    chunks = chunk_processing_result(snapshot.source)
    model_key = getattr(provider, "model_key", None)
    dimensions = getattr(provider, "dimensions", None)
    if not isinstance(model_key, str) or not model_key.strip():
        raise EmbeddingProviderError("Embedding provider is not configured")
    if dimensions != M2_EMBEDDING_DIMENSIONS:
        raise EmbeddingProviderError("Embedding provider is not configured")

    try:
        raw_vectors = provider.embed([chunk.text for chunk in chunks]) if chunks else []
    except EmbeddingProviderError:
        raise
    except Exception:
        raise EmbeddingProviderError("Embedding provider failed") from None
    vectors = validate_embeddings(
        raw_vectors,
        expected_count=len(chunks),
        dimensions=M2_EMBEDDING_DIMENSIONS,
    )

    db = session_factory()
    try:
        require_persisted_principal(db, user_id)
        try:
            # A no-op UPDATE is one portable serialization guard: PostgreSQL
            # locks this job row; SQLite serializes the write transaction.
            # OCR review updates the same job row, so eligibility cannot change
            # between this revalidation and publication.
            serialize_record(db, record_id)
            _serialize_job(db, processing_job_id)
            current = _load_source(
                db,
                record_id=record_id,
                processing_job_id=processing_job_id,
                user_id=user_id,
            )
        except (RetrievalIndexNotFound, RetrievalIndexNotReady) as error:
            raise RetrievalIndexStale("Retrieval source changed during indexing") from error
        if current.identity != snapshot.identity:
            raise RetrievalIndexStale("Retrieval source changed during indexing")

        old_chunk_ids = select(RecordChunk.id).where(
            RecordChunk.processing_job_id == processing_job_id
        )
        db.execute(delete(RecordEmbedding).where(RecordEmbedding.chunk_id.in_(old_chunk_ids)))
        db.execute(
            delete(RecordChunk).where(RecordChunk.processing_job_id == processing_job_id)
        )
        db.add_all([RecordChunk(**vars(chunk)) for chunk in chunks])
        db.add_all(
            [
                RecordEmbedding(
                    id=_embedding_id(chunk.id, model_key),
                    chunk_id=chunk.id,
                    model_key=model_key,
                    dimensions=M2_EMBEDDING_DIMENSIONS,
                    embedding=vector,
                )
                for chunk, vector in zip(chunks, vectors)
            ]
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return RetrievalIndexResponse(
        source_attempt=snapshot.source.source_attempt,
        source_result_sha256=snapshot.result_hash,
        chunk_count=len(chunks),
        embedding_count=len(vectors),
        chunker_version=snapshot.source.chunker_version,
        model_key=model_key,
    )


def index_secure_processing_result(*, runtime, user_id, record_id, processing_job_id):
    from dataclasses import asdict
    from sqlalchemy import text
    from app.api.routes.secure_operations import tx, raw_json
    from app.api.routes.secure_media import job_source, pointer, stage, abandon_staged
    from app.services import secure_catalogue as cat
    from app.services.secure_retrieval import (materialize_room, authenticated_source, source_binding,
        synthetic_embedding, revalidate_rows, MAX_CHUNKS, MAX_CIPHERTEXT)
    provider = synthetic_embedding(getattr(runtime, "embedding_provider", None), runtime=runtime)
    with tx() as db:
        from app.services.record_policy import require_record_access
        require_record_access(db, user_id, record_id, purpose="rag")
        source = job_source(db, user_id, processing_job_id)
        if source[1]["record_id"] != str(cat._uuid(record_id)):
            raise cat.SecureStateChanged()
    corpus = materialize_room(runtime, user_id, source[1]["room_id"])
    parsed = authenticated_source(runtime, source)
    chunks = chunk_processing_result(parsed)
    if sum(c.processing_job_id != processing_job_id for c in corpus.chunks) + len(chunks) > MAX_CHUNKS:
        raise OverflowError()
    if corpus.model_key is not None and corpus.model_key != provider.model_key:
        raise RetrievalIndexNotReady("Room model space differs")
    vectors = validate_embeddings(provider.embed([c.text for c in chunks]) if chunks else [],
        expected_count=len(chunks), dimensions=M2_EMBEDDING_DIMENSIONS)
    old = next((doc for oid, doc, _ in corpus.documents if oid == record_id), None)
    entries = [entry for entry in old["jobs"] if entry["job_id"] != processing_job_id] if old else []
    entries.append(source_binding(runtime, source, parsed) | dict(chunks=[asdict(c) for c in chunks], vectors=vectors))
    raw = raw_json(dict(version=1, record_id=record_id, room_id=corpus.room_id, model_key=provider.model_key,
        dimensions=M2_EMBEDDING_DIMENSIONS, jobs=sorted(entries, key=lambda e: e["job_id"])))
    previous_bytes = sum(size for oid, _, size in corpus.documents if oid == record_id)
    other_bytes = corpus.ciphertext_size - previous_bytes
    if other_bytes + len(raw) > MAX_CIPHERTEXT:
        raise OverflowError()
    target = ("record_index", uuid.UUID(record_id))
    with tx() as db:
        cat.lock_rooms(db, (corpus.room_id,))
        revalidate_rows(db, corpus)
        cat.revalidate(db, source[0])
        if dict(cat._row(db, "SELECT * FROM processing_jobs WHERE id=:j", j=processing_job_id)) != source[1]:
            raise cat.SecureStateChanged()
        if db.scalar(text("SELECT count(*) FROM protected_pointers WHERE owner_type='record_index' AND owner_id=:o"), dict(o=record_id)) == 0:
            pointer(db, "record_index", record_id, corpus.room_id)
        targets = tuple(sorted({p[:2] for p in corpus.catalogue.pointers} | {target}))
        saved = cat.snapshot(db, principal_id=uuid.UUID(user_id), targets=targets)
        candidate = uuid.uuid4()
        saved = cat.claim(db, saved, operation_id=uuid.uuid4(), target=target, candidate_object_id=candidate)
    staged = None
    try:
        staged = stage(runtime, user_id, target, raw, source=(source[2][2][0].object_id, source[2][2][0].object_version), claimed=(saved, candidate))
        # Actual A receipt includes its header, nonce and tag; raw JSON size is insufficient.
        if other_bytes + staged[3].ciphertext_size > MAX_CIPHERTEXT:
            raise OverflowError()
        with tx() as db:
            cat.lock_rooms(db, (corpus.room_id,))
            revalidate_rows(db, corpus, catalogue=saved)
            cat.publish_many(db, (staged,))
    except BaseException:
        if staged is not None:
            abandon_staged((staged,))
        raise
    return RetrievalIndexResponse(source_attempt=parsed.source_attempt, source_result_sha256=sha256_text(parsed.canonical_result_json),
        chunk_count=len(chunks), embedding_count=len(vectors), chunker_version=parsed.chunker_version, model_key=provider.model_key)
