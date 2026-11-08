"""Secure bounded media APIs. Only ciphertext reaches storage; no legacy router import."""
import hashlib
import json
from uuid import UUID, uuid4
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import bindparam, text

from app.api.routes.secure_operations import principal, raw_json, tx, create_ciphertext
from app.schemas.upload import UploadCreateRequest, UploadResponse
from app.schemas.processing import ProcessingJobCreateRequest, ProcessingJobResponse, OCRReviewRequest, OCRReviewResponse
from app.services import secure_catalogue as cat
from app.services.record_policy import allowed_record_ids, require_record_access
from app.services.media_loader import media_family
from app.services.media_validation import MIMES, read_media_upload
from app.services.processing_runner import run_secure_provider
from app.processing.contracts import ProviderError
from app.core.config import settings
from app.storage.contracts import KeyRef, PayloadIdentity, StorageReceipt
from app.storage.crypto import seal_payload, open_payload

router = APIRouter()
MAX_MEDIA = 20 * 1024 * 1024


async def multipart_bytes(request):
    return await read_media_upload(request, max_bytes=MAX_MEDIA)


def pointer(db, kind, oid, rid):
    scope = cat._row(db, "SELECT * FROM security_scopes WHERE domain='room' AND owner_id=:r", r=rid)
    cat._cas(db, "INSERT INTO protected_pointers (owner_type,owner_id,domain,scope_id,state,source_revision,deletion_epoch,revision,authorization_epoch) VALUES (:k,:o,'room',:r,'MIGRATION_PENDING',1,:de,1,:ae)",
        k=kind, o=oid, r=rid, de=scope["deletion_epoch"], ae=scope["authorization_epoch"])


def values_for(db, saved, target):
    p = next(p for p in saved.pointers if p[:2] == target)
    if p[4] is None:
        raise cat.SecureStateChanged()
    return cat.envelope_values(cat._row(db, "SELECT * FROM encrypted_objects WHERE object_id=:o AND object_version=:v", o=str(p[4]), v=p[5]))


def open_bytes(runtime, values):
    identity, ref, wrapped, receipt = values
    blob = runtime.store.read(receipt)
    if len(blob) != receipt.ciphertext_size or hashlib.sha256(blob).digest() != receipt.ciphertext_sha256:
        raise cat.SecureStateChanged()
    return open_payload(identity, blob, wrapped, runtime.vault.load(ref))


def record_source(db, uid, record_id):
    """Detached current record + optional stable file snapshot, including actual ownership."""
    record_id = str(cat._uuid(record_id))
    require_record_access(db, uid, record_id)
    row = dict(cat._row(db, "SELECT * FROM uploads WHERE id=:o", o=record_id))
    files = db.execute(text("SELECT * FROM upload_files WHERE upload_id=:o"), dict(o=record_id)).mappings().all()
    if len(files) > 1:
        raise cat.SecureStateChanged()
    target = ("record", UUID(record_id))
    targets = (target,) + ((("file", UUID(files[0]["id"])),) if files else ())
    saved = cat.snapshot(db, principal_id=UUID(uid), targets=targets)
    metadata = values_for(db, saved, target)
    original = values_for(db, saved, targets[1]) if files and next(p for p in saved.pointers if p[0] == "file")[4] else None
    return saved, row, metadata, original


def record_payload(runtime, source):
    saved, row, values, original = source
    payload = json.loads(open_bytes(runtime, values))
    if payload.get("record_id") != row["id"] or payload.get("room_id") != row["room_id"]:
        raise cat.SecureStateChanged()
    binding = payload.get("file")
    if original:
        identity = original[0]
        file_id = str(next(p[1] for p in saved.pointers if p[0] == "file"))
        if (not isinstance(binding, dict) or binding.get("file_id") != file_id
            or binding.get("object_id") != str(identity.object_id) or binding.get("object_version") != identity.object_version
            or binding.get("file_size") != identity.plaintext_length or binding.get("mime_type") not in MIMES):
            raise cat.SecureStateChanged()
    elif binding is not None:
        raise cat.SecureStateChanged()
    return payload


def original_url(record_id, file_id, identity, receipt, *, citation=None):
    params = dict(file_id=file_id, source_object_id=str(identity.object_id),
        source_object_version=identity.object_version, expected_ciphertext_sha256=receipt.ciphertext_sha256.hex())
    if citation is not None:
        params.update(processing_job_id=citation["processing_job_id"], source_attempt=citation["source_attempt"],
            source_result_sha256=citation["source_result_sha256"])
        if "authorization_epoch" in citation:
            params["authorization_epoch"] = citation["authorization_epoch"]
    query = urlencode(params)
    return f"/api/v1/uploads/{record_id}/content?{query}"


def upload_dto(request, row, payload, original=None):
    f = payload.get("file")
    return UploadResponse(upload_id=row["id"], room_id=row["room_id"], uploader_user_id=row["uploader_user_id"],
        type=row["type"], title=payload["title"], description=payload.get("description"), tags=payload.get("tags", []),
        status=row["status"], created_at=str(row["created_at"]) if row["created_at"] else None,
        has_file=f is not None, file_id=f["file_id"] if f else None, mime_type=f["mime_type"] if f else None,
        file_size=f["file_size"] if f else None,
        file_url=str(request.base_url).rstrip("/") + original_url(row["id"], f["file_id"], *original) if f else None)


def stage(runtime, uid, target, raw, *, dependencies=(), source=None, candidate=None, claimed=None):
    """Ordinary B2 journal; caller publishes returned detached tuple under the guard."""
    with tx() as db:
        if claimed is None:
            saved = cat.snapshot(db, principal_id=UUID(uid), targets=(target,) + dependencies)
            obj, operation = candidate or uuid4(), uuid4()
            saved = cat.claim(db, saved, operation_id=operation, target=target, candidate_object_id=obj)
        else:
            saved, obj = claimed
            operation = saved.operation_id
            cat.lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
            cat.revalidate(db, saved)
        domain, room, record = cat.ownership(db, *target)
        key = cat._row(db, "SELECT key_id,epoch FROM key_epochs WHERE domain=:d AND owner_id=:o AND state='ACTIVE' AND provisioning_state='PERSISTED'", d=domain, o=room)
    ref = KeyRef(domain, UUID(room), UUID(key["key_id"]), key["epoch"])
    identity = PayloadIdentity(domain, UUID(room), UUID(record), obj, 1, cat.KINDS[target[0]],
        source[0] if source else None, source[1] if source else None, len(raw))
    creating = False
    receipt = None
    try:
        sealed = seal_payload(identity, raw, runtime.vault.load(ref))
        creating = True
        receipt = create_ciphertext(runtime, saved, identity, sealed)
        with tx() as db:
            cat.record_receipt(db, saved, operation_id=operation, identity=identity, sealed=sealed, receipt=receipt)
        proof = cat.authenticate_readback(identity=identity, sealed=sealed, receipt=receipt, key_ref=ref, vault=runtime.vault, store=runtime.store)
        return saved, identity, sealed, receipt, proof
    except BaseException:
        with tx() as db:
            op = cat._row(db, "SELECT revision,state FROM secure_operations WHERE operation_id=:o", o=str(operation))
            # Any create without a durably journaled receipt remains unresolved.
            if op["state"] in ("CLAIMED", "RECEIPTED", "VERIFIED"):
                cat.abandon(db, operation_id=operation, expected_revision=op["revision"],
                    receipt_unknown=creating and op["state"] == "CLAIMED",
                    known_receipt=(receipt if op["state"] == "CLAIMED" and type(receipt) is StorageReceipt
                                   and receipt.adapter == "gcs" else None))
        raise


def abandon_staged(staged):
    with tx() as db:
        for saved, *_ in staged:
            op = cat._row(db, "SELECT revision,state FROM secure_operations WHERE operation_id=:o", o=str(saved.operation_id))
            if op["state"] in ("CLAIMED", "RECEIPTED", "VERIFIED"):
                cat.abandon(db, operation_id=saved.operation_id, expected_revision=op["revision"])


@router.delete("/uploads/{upload_id}")
async def delete_upload(upload_id: str, request: Request):
    from app.services.record_lifecycle import legacy_soft_delete

    with tx() as db:
        legacy_soft_delete(db, principal(request), upload_id)
    return {"deleted": True}


@router.post("/uploads", response_model=UploadResponse, deprecated=True)
async def create_upload(payload: UploadCreateRequest, request: Request):
    from app.services.record_service import create_secure_record
    return create_secure_record(payload, request, visibility="FAMILY")


@router.post("/uploads/{upload_id}/binary", response_model=UploadResponse)
async def upload_binary(upload_id: str, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    with tx() as db:
        if db.scalar(text("SELECT version_num FROM alembic_version")) not in ("0015_record_privacy", "0016_family_context"):
            raise HTTPException(409, "Secure upload requires the room storage quota schema")
        before = record_source(db, uid, upload_id)
    metadata = record_payload(runtime, before)
    limit = {"text": settings.text_max_input_bytes, "image": MAX_MEDIA,
             "voice": settings.media_audio_max_bytes, "video": settings.media_video_max_bytes}[before[1]["type"]]
    # M4 scope decision: retain the existing encrypted-original capacity.
    content, mime = await read_media_upload(request, max_bytes=min(limit, MAX_MEDIA))
    if mime.split("/")[0] != {"voice": "audio"}.get(before[1]["type"], before[1]["type"]):
        raise HTTPException(400, "Upload type mismatch")
    return publish_bytes(upload_id, request, before, metadata, content, mime)


@router.post("/uploads/{upload_id}/publish-text", response_model=UploadResponse)
async def publish_text(upload_id: str, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    with tx() as db:
        if db.scalar(text("SELECT version_num FROM alembic_version")) not in ("0015_record_privacy", "0016_family_context"):
            raise HTTPException(409, "Secure upload requires the room storage quota schema")
        before = record_source(db, uid, upload_id)
    if before[1]["uploader_user_id"] != uid:
        raise HTTPException(404, "Upload not found")
    if before[1]["type"] != "text":
        raise HTTPException(400, "Upload type mismatch")
    metadata = record_payload(runtime, before)
    content = (metadata.get("description") or "").encode("utf-8")
    from app.services.media_validation import validate_media
    validate_media(content, "text/plain", "source.txt")
    return publish_bytes(upload_id, request, before, metadata, content, "text/plain")


def publish_bytes(upload_id, request, before, metadata, content, mime):
    uid, runtime = principal(request), request.app.state.secure_runtime
    with tx() as db:
        cat.lock_rooms(db, (scope[1] for scope in before[0].scopes if scope[0] == "room"))
        cat.revalidate(db, before[0])
        from app.services.record_lifecycle import require_capability
        require_capability(db, uid, upload_id, "can_edit_content")
        existing = db.scalar(text("SELECT id FROM upload_files WHERE upload_id=:o"), dict(o=upload_id))
        fid = existing or str(uuid4())
        if not existing:
            cat._cas(db, "INSERT INTO upload_files (id,upload_id,encrypted,file_uploader_user_id) VALUES (:f,:o,1,:u)", f=fid, o=upload_id, u=uid)
            pointer(db, "file", fid, before[1]["room_id"])
        claims = []
        for target in (("file", UUID(fid)), ("record", UUID(upload_id))):
            saved = cat.snapshot(db, principal_id=UUID(uid), targets=(target,))
            candidate = uuid4()
            saved = cat.claim(db, saved, operation_id=uuid4(), target=target, candidate_object_id=candidate)
            claims.append((saved, candidate))
    staged = []
    try:
        staged.append(stage(runtime, uid, ("file", UUID(fid)), content, claimed=claims[0]))
        metadata["file"] = dict(file_id=fid, object_id=str(claims[0][1]), object_version=1, file_size=len(content), mime_type=mime)
        staged.append(stage(runtime, uid, ("record", UUID(upload_id)), raw_json(metadata), claimed=claims[1]))
        with tx() as db:
            require_capability(db, uid, upload_id, "can_edit_content")
            cat.publish_many(db, tuple(staged))
            cat._cas(db, "UPDATE upload_files SET file_size=:n,file_uploader_user_id=:u WHERE id=:f", n=len(content), f=fid, u=uid)
            invalidate_results(db, upload_id)
            db.execute(text("UPDATE records SET record_version=record_version+1,source_revision=source_revision+1,authorization_epoch=authorization_epoch+1 WHERE id=:r"), {"r": upload_id})
    except BaseException:
        abandon_staged(tuple((saved,) for saved, _ in claims))
        raise
    return upload_dto(request, before[1], metadata, (staged[0][1], staged[0][3]))


def invalidate_results(db, record_id):
    # Retain ciphertext/history for separately authorized B6 cleanup. No legacy row deletion.
    jobs = db.execute(text("SELECT id FROM processing_jobs WHERE record_id=:r"), dict(r=record_id)).scalars().all()
    invalidate_pointers(db, [("job", j) for j in jobs] + [("record_index", record_id)])
    db.execute(text("UPDATE processing_jobs SET status='FAILED',failure_code='source_changed',failure_message=NULL,result_json=NULL,review_json=NULL,attempt=attempt+1,updated_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP WHERE record_id=:r"), dict(r=record_id))


def invalidate_pointers(db, targets):
    for kind, oid in targets:
        p = db.execute(text("SELECT * FROM protected_pointers WHERE owner_type=:k AND owner_id=:o"), dict(k=kind, o=oid)).mappings().one_or_none()
        if p:
            if p["operation_id"]:
                op = cat._row(db, "SELECT state,revision FROM secure_operations WHERE operation_id=:o", o=p["operation_id"])
                if op["state"] in ("CLAIMED", "RECEIPTED", "VERIFIED"):
                    # A foreign live claim is evidence of inconsistent serialization, not permission to steal it.
                    raise cat.SecureStateChanged()
            cat._cas(db, "UPDATE protected_pointers SET object_id=NULL,object_version=NULL,state='MIGRATION_PENDING',operation_id=NULL,source_revision=source_revision+1,revision=revision+1 WHERE owner_type=:k AND owner_id=:o AND revision=:v", k=kind, o=oid, v=p["revision"])
            if p["object_id"]:
                db.execute(text("UPDATE encrypted_objects SET state='RETIRED',envelope_revision=envelope_revision+1 WHERE object_id=:o AND object_version=:v AND state='ACTIVE' AND NOT EXISTS (SELECT 1 FROM protected_pointers WHERE object_id=:o AND object_version=:v)"), dict(o=p["object_id"], v=p["object_version"]))


@router.get("/uploads/{upload_id}/content")
async def get_content(upload_id: str, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    with tx() as db:
        source = record_source(db, uid, upload_id)
    if source[3] is None:
        raise HTTPException(404, "Upload file not found")
    identity, _, _, receipt = source[3]
    expected = dict(file_id=str(next(p[1] for p in source[0].pointers if p[0] == "file")),
        source_object_id=str(identity.object_id), source_object_version=str(identity.object_version),
        expected_ciphertext_sha256=receipt.ciphertext_sha256.hex())
    if any(request.query_params.getlist(key) != [value] for key, value in expected.items()):
        raise HTTPException(409, "STALE_SOURCE")
    cited = None
    citation_keys = ("processing_job_id", "source_attempt", "source_result_sha256")
    if any(key in request.query_params for key in citation_keys):
        try:
            if any(len(request.query_params.getlist(key)) != 1 for key in citation_keys):
                raise ValueError()
            from app.services.secure_retrieval import authenticated_source
            from app.retrieval.chunking import sha256_text
            with tx() as db:
                policy = require_record_access(db, uid, upload_id)
                if policy["lifecycle_state"] != "ACTIVE" or policy["review_status"] != "APPROVED":
                    raise ValueError()
                expected_version = request.query_params.get("authorization_epoch")
                if expected_version is not None and expected_version != str(policy["authorization_epoch"]):
                    raise ValueError()
                cited = job_source(db, uid, request.query_params["processing_job_id"])
            parsed = authenticated_source(runtime, cited)
            if (cited[1]["record_id"] != upload_id or cited[1]["upload_file_id"] != expected["file_id"]
                    or str(cited[1]["attempt"]) != request.query_params["source_attempt"]
                    or sha256_text(parsed.canonical_result_json) != request.query_params["source_result_sha256"]):
                raise ValueError()
        except (ValueError, KeyError, cat.SecureStateChanged):
            raise HTTPException(409, "STALE_SOURCE") from None
    if "claim_id" in request.query_params or "claim_version" in request.query_params:
        from app.services.claims import validate_citation
        try:
            if any(len(request.query_params.getlist(k)) != 1 for k in ("claim_id", "claim_version")):
                raise ValueError()
            validate_citation(runtime, uid, source[1]["room_id"], upload_id,
                              request.query_params["claim_id"], int(request.query_params["claim_version"]), request.query_params)
        except (ValueError, KeyError):
            raise HTTPException(409, "STALE_SOURCE") from None
    metadata = record_payload(runtime, source)
    content = open_bytes(runtime, source[3])
    family = media_family(content)
    if metadata["file"]["mime_type"] == "text/plain":
        from app.processing.native_text import decode_native_text
        decode_native_text(content)
    elif family is None or metadata["file"]["mime_type"].split("/")[0] not in family:
        raise cat.SecureStateChanged()
    with tx() as db:
        cat.revalidate(db, source[0])
        if cited is not None:
            cat.revalidate(db, cited[0])
            if dict(cat._row(db, "SELECT * FROM processing_jobs WHERE id=:j", j=cited[1]["id"])) != cited[1]:
                raise cat.SecureStateChanged()
    request.scope["secure_media"] = True
    return Response(content, media_type=metadata["file"]["mime_type"])


@router.get("/uploads/{room_id}", response_model=list[UploadResponse])
async def list_uploads(room_id: str, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    with tx() as db:
        saved = cat.snapshot(db, principal_id=UUID(uid), targets=(("room", cat._uuid(room_id)),))
        authorized = allowed_record_ids(db, uid, room_id, purpose="browse")
        # Logical deletion retains pointers and ciphertext for audited cleanup.
        # Use actual upload ownership, not the pointer's claimed scope, as authority.
        ids = db.execute(text(
            "SELECT u.id FROM uploads u JOIN protected_pointers p "
            "ON p.owner_type='record' AND p.owner_id=u.id "
            "WHERE u.room_id=:r AND u.id IN :authorized AND p.state='ENCRYPTED_ACTIVE' "
            "AND NOT EXISTS (SELECT 1 FROM security_tombstones t "
            "WHERE t.domain='room' AND t.owner_id=u.room_id "
            "AND t.target_kind='record' AND t.target_id=u.id) "
            "ORDER BY u.created_at DESC,u.id DESC"
        ).bindparams(bindparam("authorized", expanding=True)), dict(r=room_id, authorized=authorized)).scalars().all()
        if len(ids) > 1000:
            raise HTTPException(413, "Response too large")
        sources = [record_source(db, uid, oid) for oid in ids]
    dtos = [upload_dto(request, s[1], record_payload(runtime, s), (s[3][0], s[3][3]) if s[3] else None) for s in sources]
    if len(raw_json([d.model_dump() for d in dtos])) > 4 * 1024 * 1024:
        raise HTTPException(413, "Response too large")
    with tx() as db:
        cat.revalidate(db, saved)
        for source in sources:
            cat.revalidate(db, source[0])
    return dtos


FAILURES = {"UNSUPPORTED_MEDIA", "DECODE_ERROR", "OCR_ERROR", "STT_ERROR", "VLM_ERROR", "source_changed", "processor_not_configured", "provider_forbidden", "provider_timeout", "cancelled", "unexpected_error", "abandoned", "invalid_text", "text_too_large", "text_output_too_large"}


def job_source(db, uid, job_id):
    found = db.execute(text("SELECT * FROM processing_jobs WHERE id=:j"), {"j": str(cat._uuid(job_id))}).mappings().first()
    if found is None:
        from app.services.record_policy import RecordAccessDenied
        raise RecordAccessDenied(uid, job_id)
    row = dict(found)
    source = record_source(db, uid, row["record_id"])
    targets = tuple(p[:2] for p in source[0].pointers) + (("job", UUID(job_id)),)
    saved = cat.snapshot(db, principal_id=UUID(uid), targets=targets)
    if source[3] is None or ("file", UUID(row["upload_file_id"])) not in targets:
        raise cat.SecureStateChanged()
    p = next(p for p in saved.pointers if p[0] == "job")
    result = values_for(db, saved, p[:2]) if p[4] else None
    return saved, row, source, result


def job_dto(row, result=None):
    if row["failure_code"] is not None and row["failure_code"] not in FAILURES:
        raise cat.SecureStateChanged()
    return ProcessingJobResponse(job_id=row["id"], record_id=row["record_id"], room_id=row["room_id"],
        upload_file_id=row["upload_file_id"], processor=row["processor"], status=row["status"], attempt=row["attempt"],
        result=result, failure_code=row["failure_code"], failure_message="Processing failed." if row["failure_code"] else None,
        **{k: str(row[k]) if row[k] else None for k in ("created_at", "updated_at", "started_at", "completed_at")})


def result_binding(row, source):
    return dict(job_id=row["id"], record_id=row["record_id"], room_id=row["room_id"], file_id=row["upload_file_id"],
        attempt=row["attempt"], source_object_id=str(source[3][0].object_id), source_version=source[3][0].object_version,
        metadata_object_id=str(source[2][0].object_id), metadata_version=source[2][0].object_version)


def validate_result_binding(runtime, row, source, payload):
    expected = result_binding(row, source)
    metadata_fields = {"metadata_object_id", "metadata_version"}
    if any(payload.get(key) != value for key, value in expected.items() if key not in metadata_fields):
        raise cat.SecureStateChanged()
    if all(payload.get(key) == expected[key] for key in metadata_fields):
        return
    with tx() as db:
        cat.revalidate(db, source[0])
        historical = cat._row(db,
            "SELECT * FROM encrypted_objects WHERE object_id=:o AND object_version=:v "
            "AND domain='room' AND owner_id=:room AND record_id=:record "
            "AND artifact_kind='record_metadata' AND state IN ('ACTIVE','RETIRED')",
            o=str(cat._uuid(payload.get("metadata_object_id"))),
            v=cat._count(payload.get("metadata_version")), room=row["room_id"], record=row["record_id"])
        envelope = cat.envelope_values(historical)
    old = json.loads(open_bytes(runtime, envelope))
    current = record_payload(runtime, source)
    fields = ("record_id", "room_id", "description", "tags", "file")
    if any(old.get(key) != current.get(key) for key in fields):
        raise cat.SecureStateChanged()
    old_semantics = {key: value for key, value in old.get("m2_metadata", {}).items() if key != "title"}
    new_semantics = {key: value for key, value in current.get("m2_metadata", {}).items() if key != "title"}
    if old_semantics != new_semantics:
        raise cat.SecureStateChanged()


def read_job(runtime, uid, job_id):
    with tx() as db:
        saved, row, source, values = job_source(db, uid, job_id)
    record_payload(runtime, source)
    result = None
    if row["status"] == "SUCCEEDED":
        if values is None:
            raise cat.SecureStateChanged()
        payload = json.loads(open_bytes(runtime, values))
        validate_result_binding(runtime, row, source, payload)
        if (values[0].source_object_id, values[0].source_version) != (source[3][0].object_id, source[3][0].object_version):
            raise cat.SecureStateChanged()
        from app.services.multimodal_jobs import visible_result
        result = visible_result(payload["result"], uid=uid, room_id=row["room_id"], runtime=runtime)
    with tx() as db:
        cat.revalidate(db, saved)
        if dict(cat._row(db, "SELECT * FROM processing_jobs WHERE id=:j", j=job_id)) != row:
            raise cat.SecureStateChanged()
    return job_dto(row, result)


@router.post("/records/{record_id}/processing-jobs", response_model=ProcessingJobResponse)
async def create_job(record_id: str, payload: ProcessingJobCreateRequest, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    if payload.processor not in ("ocr", "stt", "text"):
        raise HTTPException(400, "Unsupported processor")
    with tx() as db:
        source = record_source(db, uid, record_id)
    metadata = record_payload(runtime, source)
    file = metadata.get("file")
    if not file or file["file_id"] != payload.upload_file_id:
        raise HTTPException(404, "Upload file not found")
    allowed_families = {"ocr": ("image",), "stt": ("audio", "video"), "text": ("text",)}
    if (file["mime_type"].split("/")[0] not in allowed_families[payload.processor]
            or payload.processor == "text" and file["mime_type"] != "text/plain"):
        raise HTTPException(400, "Unsupported media type")
    with tx() as db:
        cat.lock_rooms(db, (scope[1] for scope in source[0].scopes if scope[0] == "room"))
        cat.revalidate(db, source[0])
        jid = db.scalar(text("SELECT id FROM processing_jobs WHERE record_id=:r AND upload_file_id=:f AND processor=:p"),
            dict(r=record_id, f=file["file_id"], p=payload.processor))
        if jid is None:
            jid = str(uuid4())
            cat._cas(db, "INSERT INTO processing_jobs (id,record_id,room_id,upload_file_id,processor,status,attempt) VALUES (:j,:r,:room,:f,:p,'PENDING',0)",
                j=jid, r=record_id, room=source[1]["room_id"], f=file["file_id"], p=payload.processor)
            pointer(db, "job", jid, source[1]["room_id"])
            cat._cas(db, "UPDATE security_scopes SET revision=revision+1 WHERE domain='room' AND owner_id=:r", r=source[1]["room_id"])
    return read_job(runtime, uid, jid)


@router.get("/processing-jobs/{job_id}", response_model=ProcessingJobResponse)
async def get_job(job_id: str, request: Request):
    return read_job(request.app.state.secure_runtime, principal(request), job_id)


@router.post("/processing-jobs/{job_id}/run", response_model=ProcessingJobResponse)
async def run_job(job_id: str, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    with tx() as db:
        saved, row, source, _ = job_source(db, uid, job_id)
        cat.lock_rooms(db, (row["room_id"],))
        saved, row, source, _ = job_source(db, uid, job_id)
        if row["status"] != "PENDING":
            raise HTTPException(409, "Processing state changed")
        cat._cas(db, "UPDATE processing_jobs SET status='RUNNING',started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,completed_at=NULL WHERE id=:j AND status='PENDING' AND attempt=:a", j=job_id, a=row["attempt"])
        obj = uuid4()
        saved = cat.claim(db, saved, operation_id=uuid4(), target=("job", UUID(job_id)), candidate_object_id=obj)
    staged = None
    try:
        metadata = record_payload(runtime, source)
        content = open_bytes(runtime, source[3])
        family = media_family(content)
        mime = metadata["file"]["mime_type"]
        if row["processor"] != "text" and (family is None or mime.split("/")[0] not in family):
            raise cat.SecureStateChanged()
        failure = None
        try:
            if request.scope.get("m4_processing") is True:
                from app.services.multimodal_jobs import extract_configured
                result = extract_configured(content, mime, runtime=runtime,
                    attempt_id=f"{job_id}:{row['attempt']}")
            else:
                result = await run_secure_provider(runtime, request.scope["secure_guard"], row["processor"], content, mime)
            payload = raw_json(result_binding(row, source) | {"result": result})
            if len(payload) > 1024 * 1024:
                raise ValueError()
        except ProviderError as exc:
            failure = exc.code if exc.code in FAILURES else "unexpected_error"
        except Exception:
            failure = "unexpected_error"
        with tx() as db:
            cat.lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
            cat.revalidate(db, saved)
            current = cat._row(db, "SELECT * FROM processing_jobs WHERE id=:j", j=job_id)
            if current["status"] != "RUNNING" or current["attempt"] != row["attempt"]:
                raise cat.SecureStateChanged()
        if failure:
            with tx() as db:
                cat.lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
                cat.revalidate(db, saved)
                cat.abandon(db, operation_id=saved.operation_id, expected_revision=1)
                cat._cas(db, "UPDATE processing_jobs SET status='FAILED',failure_code=:f,failure_message=NULL,result_json=NULL,updated_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP WHERE id=:j AND status='RUNNING' AND attempt=:a", f=failure, j=job_id, a=row["attempt"])
                cat._cas(db, "UPDATE security_scopes SET revision=revision+1 WHERE domain='room' AND owner_id=:r", r=row["room_id"])
        else:
            staged = stage(runtime, uid, ("job", UUID(job_id)), payload, source=(source[3][0].object_id, source[3][0].object_version), claimed=(saved, obj))
            with tx() as db:
                cat.lock_rooms(db, (row["room_id"],))
                cat._cas(db, "UPDATE processing_jobs SET status='SUCCEEDED',failure_code=NULL,failure_message=NULL,result_json=NULL,updated_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP WHERE id=:j AND status='RUNNING' AND attempt=:a", j=job_id, a=row["attempt"])
                cat.publish_many(db, (staged,))
                if request.scope.get("m4_processing") is True:
                    from app.services.record_service import save_idempotency
                    save_idempotency(db, request, "{}", job_id, job_id)
    except BaseException:
        with tx() as db:
            op = cat._row(db, "SELECT revision,state FROM secure_operations WHERE operation_id=:o", o=str(saved.operation_id))
            if op["state"] in ("CLAIMED", "RECEIPTED", "VERIFIED"):
                cat.abandon(db, operation_id=saved.operation_id, expected_revision=op["revision"])
        raise
    return read_job(runtime, uid, job_id)


@router.post("/processing-jobs/{job_id}/retry", response_model=ProcessingJobResponse)
async def retry_job(job_id: str, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    with tx() as db:
        saved, row, source, _ = job_source(db, uid, job_id)
        cat.lock_rooms(db, (row["room_id"],))
        saved, row, source, _ = job_source(db, uid, job_id)
        if row["status"] == "RUNNING":
            # Never infer provider drain from elapsed time. A terminated invocation
            # or an explicitly abandoned claim must be established under this guard.
            p = cat._row(db, "SELECT operation_id FROM protected_pointers WHERE owner_type='job' AND owner_id=:j", j=job_id)
            op = cat._row(db, "SELECT * FROM secure_operations WHERE operation_id=:o", o=p["operation_id"])
            if op["state"] not in ("ABANDONED",):
                raise HTTPException(409, "Processing recovery requires reconciliation")
            cat._cas(db, "UPDATE processing_jobs SET status='FAILED',failure_code='abandoned',failure_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=:j AND status='RUNNING' AND attempt=:a", j=job_id, a=row["attempt"])
        elif row["status"] != "FAILED":
            raise HTTPException(409, "Processing state changed")
        cat._cas(db, "UPDATE processing_jobs SET status='PENDING',attempt=attempt+1,failure_code=NULL,failure_message=NULL,result_json=NULL,started_at=NULL,completed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=:j AND status='FAILED' AND attempt=:a", j=job_id, a=row["attempt"])
        cat._cas(db, "UPDATE protected_pointers SET source_revision=source_revision+1,revision=revision+1 WHERE owner_type='job' AND owner_id=:j", j=job_id)
        cat._cas(db, "UPDATE security_scopes SET revision=revision+1 WHERE domain='room' AND owner_id=:r", r=row["room_id"])
    return read_job(runtime, uid, job_id)


def review_source(db, uid, job_id):
    saved, row, source, values = job_source(db, uid, job_id)
    file = dict(cat._row(db, "SELECT * FROM upload_files WHERE id=:f", f=row["upload_file_id"]))
    if file.get("file_uploader_user_id") is None:
        raise HTTPException(409, "Actual file uploader is unknown")
    if file["file_uploader_user_id"] != uid:
        raise HTTPException(404, "Processing job not found")
    if row["processor"] not in ("ocr", "stt", "text"):
        raise HTTPException(400, "Only extraction results can be reviewed")
    if row["status"] != "SUCCEEDED" or values is None:
        raise HTTPException(409, "OCR result is incomplete or superseded")
    if "review_json" not in row:
        raise HTTPException(409, "OCR review schema is required")
    return saved, row, source, values, file


def secure_ocr_review(runtime, uid, job_id, action=None):
    from app.retrieval.chunking import canonical_json, sha256_text

    with tx() as db:
        saved, row, source, values, file = review_source(db, uid, job_id)
    record_payload(runtime, source)
    payload = json.loads(open_bytes(runtime, values))
    validate_result_binding(runtime, row, source, payload)
    if ((values[0].source_object_id, values[0].source_version) !=
            (source[3][0].object_id, source[3][0].object_version)):
        raise cat.SecureStateChanged()
    try:
        result = payload.get("result")
        if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                or result.get("extractor") != {"ocr": "AUTO_OCR", "stt": "AUTO_STT", "text": "NATIVE_TEXT"}[row["processor"]]
                or not isinstance(result.get("extractor_id"), str) or not result["extractor_id"].strip()):
            raise ValueError()
        json.dumps(result, allow_nan=False)
        digest = sha256_text(canonical_json(result))
        review = json.loads(row["review_json"]) if row["review_json"] is not None else None
        if review is not None and (not isinstance(review, dict)
                or set(review) != {"status", "source_attempt", "source_result_sha256"}
                or review["status"] not in ("confirmed", "rejected")
                or type(review["source_attempt"]) is not int or review["source_attempt"] != row["attempt"]
                or review["source_result_sha256"] != digest):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(409, "OCR result or review source is invalid") from None
    status = review["status"] if review else "unreviewed"
    target_status = {"approve": "confirmed", "reject": "rejected"}.get(action, status)
    if review is not None and target_status != status:
        raise HTTPException(409, "OCR review is already final")
    with tx() as db:
        cat.lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
        cat.revalidate(db, saved)
        current = review_source(db, uid, job_id)
        if current[1] != row or current[4] != file:
            raise cat.SecureStateChanged()
        if action is not None and review is None:
            cat._cas(db, "UPDATE processing_jobs SET review_json=:r WHERE id=:j AND review_json IS NULL AND attempt=:a AND status='SUCCEEDED'",
                r=canonical_json(dict(status=target_status, source_attempt=row["attempt"], source_result_sha256=digest)),
                j=job_id, a=row["attempt"])
            if target_status == "rejected":
                invalidate_pointers(db, [("record_index", row["record_id"])])
            cat._cas(db, "UPDATE security_scopes SET revision=revision+1 WHERE domain='room' AND owner_id=:r", r=row["room_id"])
    return dict(job_id=job_id, review_status=target_status, user_confirmed=target_status == "confirmed",
        source_attempt=row["attempt"], source_result_sha256=digest)


@router.get("/processing-jobs/{job_id}/review", response_model=OCRReviewResponse)
async def get_ocr_review(job_id: str, request: Request):
    return secure_ocr_review(request.app.state.secure_runtime, principal(request), job_id)


@router.post("/processing-jobs/{job_id}/review", response_model=OCRReviewResponse)
async def post_ocr_review(job_id: str, payload: OCRReviewRequest, request: Request):
    return secure_ocr_review(request.app.state.secure_runtime, principal(request), job_id, payload.action)
