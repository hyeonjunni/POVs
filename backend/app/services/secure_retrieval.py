"""Bounded, detached encrypted room corpus. No plaintext retrieval SQL or providers."""
import json
from dataclasses import asdict, dataclass
from time import perf_counter_ns
from uuid import UUID

from sqlalchemy import bindparam, text

from app.api.routes.secure_operations import read_room, tx
from app.api.routes.secure_media import job_source, record_source, record_payload, open_bytes, values_for, result_binding, validate_result_binding
from app.models.retrieval import M2_EMBEDDING_DIMENSIONS
from app.retrieval.chunking import ChunkSource, chunk_processing_result, normalize_text, sha256_text
from app.retrieval.embeddings import EmbeddingProviderError, validate_embeddings
from app.services import secure_catalogue as cat
from app.services.record_policy import allowed_record_ids, require_record_access

MAX_CHUNKS = 1000
MAX_CIPHERTEXT = 32 * 1024 * 1024


def synthetic_embedding(provider, *, runtime=None):
    # Exact inert type: subclasses/remote adapters cannot cross this B-only boundary.
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    from app.services.secure_local_providers import LocalEmbedding
    if (runtime is not None and getattr(runtime, "local_provider_admission", False) is True
            and type(provider) is LocalEmbedding and provider.model_key == "bge-m3:latest"
            and provider.dimensions == M2_EMBEDDING_DIMENSIONS):
        return provider
    if (type(provider) is not SyntheticTokenHashProvider or provider.dimensions != M2_EMBEDDING_DIMENSIONS
            or not isinstance(provider.model_key, str) or not provider.model_key.strip()):
        raise EmbeddingProviderError("Embedding provider is not configured")
    return provider


def authenticated_source(runtime, source):
    _, row, record, values = source
    record_payload(runtime, record)
    if row["status"] != "SUCCEEDED" or values is None or row["attempt"] < 0:
        raise cat.SecureStateChanged()
    payload = json.loads(open_bytes(runtime, values))
    validate_result_binding(runtime, row, record, payload)
    if ((values[0].source_object_id, values[0].source_version) !=
            (record[3][0].object_id, record[3][0].object_version)):
        raise cat.SecureStateChanged()
    result = payload.get("result")
    from types import SimpleNamespace
    from app.services.processing_jobs import processing_result_is_evidence
    evidence_job = SimpleNamespace(status=row["status"], processor=row["processor"],
        attempt=row["attempt"], review_json=row.get("review_json"),
        result_json=json.dumps(result, allow_nan=False))
    if not processing_result_is_evidence(evidence_job):
        raise cat.SecureStateChanged()
    if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
            or result.get("extractor") != {"ocr": "AUTO_OCR", "stt": "AUTO_STT", "text": "NATIVE_TEXT"}.get(row["processor"])
            or not isinstance(result.get("extractor_id"), str) or not result["extractor_id"].strip()
            or result.get("user_confirmed") is not False or result.get("review_status") != "unreviewed"):
        raise cat.SecureStateChanged()
    return ChunkSource(room_id=row["room_id"], record_id=row["record_id"], file_id=row["upload_file_id"],
        processing_job_id=row["id"], source_attempt=row["attempt"], result=result)


def source_binding(runtime, source, parsed):
    _, row, record, values = source
    payload = json.loads(open_bytes(runtime, values))
    validate_result_binding(runtime, row, record, payload)
    binding = result_binding(row, record) | {key: payload[key] for key in ("metadata_object_id", "metadata_version")}
    return binding | dict(result_object_id=str(values[0].object_id),
        result_version=values[0].object_version, source_result_sha256=sha256_text(parsed.canonical_result_json),
        chunker_version=parsed.chunker_version)


@dataclass(frozen=True)
class SecureRoomCorpus:
    catalogue: cat.CatalogueSnapshot
    room_id: str
    membership: tuple
    records: tuple
    jobs: tuple
    documents: tuple
    chunks: tuple
    vectors: tuple
    model_key: str | None
    ciphertext_size: int
    member_names: tuple
    record_texts: tuple


def revalidate_rows(db, corpus, *, catalogue=None):
    cat.revalidate(db, catalogue or corpus.catalogue)
    membership = tuple(tuple(r) for r in db.execute(text(
        "SELECT user_id,role,related_to_user_id FROM family_members WHERE room_id=:r ORDER BY user_id"), dict(r=corpus.room_id)))
    if membership != corpus.membership:
        raise cat.SecureStateChanged()
    for source in corpus.records:
        require_record_access(db, str(corpus.catalogue.principal_id), source[1]["id"], purpose="rag")
        if dict(cat._row(db, "SELECT * FROM uploads WHERE id=:o", o=source[1]["id"])) != source[1]:
            raise cat.SecureStateChanged()
    for source in corpus.jobs:
        if dict(cat._row(db, "SELECT * FROM processing_jobs WHERE id=:j", j=source[1]["id"])) != source[1]:
            raise cat.SecureStateChanged()


def materialize_room(runtime, uid, room_id, *, timings=None):
    """Capture the complete room before decrypting; never truncate the corpus to k."""
    started = perf_counter_ns()
    room_id = str(cat._uuid(room_id))
    room, room_saved, membership = read_room(runtime, uid, room_id)
    with tx() as db:
        cat.revalidate(db, room_saved)
        authorized = allowed_record_ids(db, uid, room_id, purpose="rag")
        ids = db.execute(text("SELECT u.id FROM uploads u JOIN protected_pointers p ON p.owner_type='record' AND p.owner_id=u.id WHERE u.room_id=:r AND u.id IN :authorized AND p.state='ENCRYPTED_ACTIVE' AND NOT EXISTS (SELECT 1 FROM security_tombstones t WHERE t.domain=p.domain AND t.owner_id=p.scope_id AND t.target_kind='record' AND t.target_id=u.id) ORDER BY u.id LIMIT 1001").bindparams(bindparam("authorized", expanding=True)), dict(r=room_id, authorized=authorized)).scalars().all()
        if len(ids) > 1000:
            raise OverflowError()
        records = tuple(record_source(db, uid, oid) for oid in ids)
        job_ids = db.execute(text("SELECT j.id FROM processing_jobs j JOIN protected_pointers p ON p.owner_type='record' AND p.owner_id=j.record_id WHERE j.room_id=:r AND j.record_id IN :authorized AND p.state='ENCRYPTED_ACTIVE' AND NOT EXISTS (SELECT 1 FROM security_tombstones t WHERE t.domain=p.domain AND t.owner_id=p.scope_id AND t.target_kind='record' AND t.target_id=j.record_id) ORDER BY j.id LIMIT 2001").bindparams(bindparam("authorized", expanding=True)), dict(r=room_id, authorized=authorized)).scalars().all()
        if len(job_ids) > 2000:
            raise OverflowError()
        jobs = tuple(job_source(db, uid, jid) for jid in job_ids)
        indexes = db.execute(text("SELECT p.owner_id,p.state FROM protected_pointers p WHERE p.owner_type='record_index' AND p.scope_id=:r AND p.owner_id IN :authorized AND NOT EXISTS (SELECT 1 FROM security_tombstones t WHERE t.domain=p.domain AND t.owner_id=p.scope_id AND t.target_kind='record' AND t.target_id=p.owner_id) ORDER BY p.owner_id LIMIT 1001").bindparams(bindparam("authorized", expanding=True)), dict(r=room_id, authorized=authorized)).all()
        if len(indexes) > 1000:
            raise OverflowError()
        targets = {("room", UUID(room_id))}
        for source in (*records, *jobs):
            targets.update(p[:2] for p in source[0].pointers)
        targets.update(("record_index", UUID(oid)) for oid, _ in indexes)
        saved = cat.snapshot(db, principal_id=UUID(uid), targets=tuple(sorted(targets)))
        envelopes = tuple((oid, values_for(db, saved, ("record_index", UUID(oid)))) for oid, state in indexes if state == "ENCRYPTED_ACTIVE")
        index_sources = {}
        for oid, values in envelopes:
            historical = cat._row(db,
                "SELECT * FROM encrypted_objects WHERE object_id=:o AND object_version=:v "
                "AND domain='room' AND owner_id=:room AND record_id=:record "
                "AND artifact_kind='record_metadata' AND state IN ('ACTIVE','RETIRED')",
                o=str(values[0].source_object_id), v=values[0].source_version,
                room=room_id, record=oid)
            index_sources[oid] = cat.envelope_values(historical)
        total_bytes = sum(values[3].ciphertext_size for _, values in envelopes)
        if total_bytes > MAX_CIPHERTEXT:
            raise OverflowError()
    if timings is not None:
        timings["catalogue_load_ns"] = perf_counter_ns() - started
    crypto_started = perf_counter_ns()
    metadata = {s[1]["id"]: record_payload(runtime, s) for s in records}
    job_by_id = {s[1]["id"]: s for s in jobs}
    record_by_id = {s[1]["id"]: s for s in records}
    documents, chunks, vectors, models = [], [], [], set()
    for oid, values in envelopes:
        record = record_by_id.get(oid)
        if record is None:
            raise cat.SecureStateChanged()
        if (values[0].source_object_id, values[0].source_version) != (record[2][0].object_id, record[2][0].object_version):
            indexed_metadata = json.loads(open_bytes(runtime, index_sources[oid]))
            current_metadata = metadata[oid]
            binding_fields = ("record_id", "room_id", "description", "tags", "file")
            if any(indexed_metadata.get(field) != current_metadata.get(field) for field in binding_fields):
                raise cat.SecureStateChanged()
            old_semantics = {k: v for k, v in indexed_metadata.get("m2_metadata", {}).items() if k != "title"}
            new_semantics = {k: v for k, v in current_metadata.get("m2_metadata", {}).items() if k != "title"}
            if old_semantics != new_semantics:
                raise cat.SecureStateChanged()
        doc = json.loads(open_bytes(runtime, values))
        if (set(doc) != {"version", "record_id", "room_id", "model_key", "dimensions", "jobs"}
                or doc["version"] != 1 or doc["record_id"] != oid or doc["room_id"] != room_id
                or type(doc["dimensions"]) is not int or doc["dimensions"] != M2_EMBEDDING_DIMENSIONS
                or not isinstance(doc["model_key"], str) or not doc["model_key"].strip()
                or not isinstance(doc["jobs"], list) or not doc["jobs"] or len(doc["jobs"]) > 2):
            raise cat.SecureStateChanged()
        models.add(doc["model_key"])
        if len(models) > 1:
            raise cat.SecureStateChanged()
        seen = set()
        for entry in doc["jobs"]:
            source = job_by_id.get(entry.get("job_id"))
            if source is None or source[1]["record_id"] != oid or source[1]["id"] in seen:
                raise cat.SecureStateChanged()
            seen.add(source[1]["id"])
            parsed = authenticated_source(runtime, source)
            binding = source_binding(runtime, source, parsed)
            if set(entry) != {*binding, "chunks", "vectors"} or any(entry.get(k) != v for k, v in binding.items()):
                raise cat.SecureStateChanged()
            expected = chunk_processing_result(parsed)
            if len(chunks) + len(expected) > MAX_CHUNKS:
                raise OverflowError()
            if entry["chunks"] != [asdict(chunk) for chunk in expected]:
                raise cat.SecureStateChanged()
            for chunk in expected:
                if normalize_text(parsed.result["text"][chunk.char_start:chunk.char_end]) != chunk.text:
                    raise cat.SecureStateChanged()
            checked = validate_embeddings(entry["vectors"], expected_count=len(expected), dimensions=M2_EMBEDDING_DIMENSIONS)
            chunks.extend(expected)
            vectors.extend((chunk, tuple(vector)) for chunk, vector in zip(expected, checked))
        documents.append((oid, doc, values[3].ciphertext_size))
    if len({c.id for c in chunks}) != len(chunks):
        raise cat.SecureStateChanged()
    from app.ai.demo_service import secure_room_entity_sources
    member_names, record_texts = secure_room_entity_sources(room, metadata.values())
    corpus = SecureRoomCorpus(saved, room_id, membership, records, jobs, tuple(documents), tuple(chunks),
        tuple(vectors), next(iter(models), None), total_bytes, tuple(member_names), tuple(record_texts))
    if timings is not None:
        timings["crypto_materialization_ns"] = perf_counter_ns() - crypto_started
    with tx() as db:
        revalidate_rows(db, corpus)
    if timings is not None:
        timings["materialization_ns"] = perf_counter_ns() - started
    return corpus


def revalidate_evidence(runtime, corpus):
    """Authenticate source bytes again outside SQL, then validate every opaque dependency."""
    expected = {}
    for source in corpus.jobs:
        if any(c.processing_job_id == source[1]["id"] for c in corpus.chunks):
            parsed = authenticated_source(runtime, source)
            expected.update((c.id, c) for c in chunk_processing_result(parsed))
    if any(expected.get(c.id) != c for c in corpus.chunks):
        raise cat.SecureStateChanged()
    with tx() as db:
        revalidate_rows(db, corpus)
