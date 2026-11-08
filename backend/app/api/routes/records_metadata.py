import hashlib
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from app.api.routes.secure_operations import principal, raw_json, tx
from app.schemas.record import MetadataPatch, RecordAction
from app.services.record_lifecycle import (
    begin_mutation,
    finish_mutation,
    invalidate_evidence,
    require_capability,
)
from app.services.record_policy import require_record_access
from app.services.record_service import validate_members

router = APIRouter()


@router.patch("/records/{record_id}/metadata")
async def patch_metadata(record_id: str, payload: MetadataPatch, request: Request):
    from app.api.routes.secure_media import (
        abandon_staged,
        record_payload,
        record_source,
        stage,
    )
    from app.services import secure_catalogue as cat
    from app.services.record_service_response import record_response
    uid = principal(request)
    changes = payload.changes.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, {"code": "EMPTY_CHANGES"})
    with tx() as db:
        before = require_record_access(db, uid, record_id)
        require_capability(db, uid, record_id, "can_edit_metadata")
        expected = request.headers.get("if-match")
        if expected is None:
            raise HTTPException(428, {"code": "PRECONDITION_REQUIRED"})
        if expected != f'"{before["record_version"]}"':
            raise HTTPException(412, {"code": "REVISION_CONFLICT"})
        owner_fields = {"title", "source_record_id", "authored_at", "record_kind",
                        "source_provenance", "image_metadata", "audio_metadata"}
        if owner_fields.intersection(changes) and before["owner_member_id"] != before["actor_member_id"]:
            raise HTTPException(403, {"code": "FIELD_NOT_ALLOWED"})
        if changes.get("title", "") is None:
            raise HTTPException(422, {"code": "TITLE_REQUIRED"})
        for field in ("subject_member_ids", "participant_member_ids"):
            if field in changes:
                validate_members(db, before["room_id"], changes[field] or [])
        source = record_source(db, uid, record_id) if getattr(request.app.state, "secure_runtime", None) is not None else None
        legacy_title = db.scalar(text("SELECT title FROM uploads WHERE id=:r"), {"r": record_id}) if source is None else None
    staged = None
    legacy_metadata = {}
    legacy_history = None
    if source:
        runtime = request.app.state.secure_runtime
        metadata = record_payload(runtime, source)
        history = {"before": metadata.get("m2_metadata", {}), "title_before": metadata["title"], "changes": changes, "reason": payload.reason}
        metadata["m2_metadata"] = {**metadata.get("m2_metadata", {}), **changes}
        if "title" in changes:
            metadata["title"] = changes["title"]
        metadata["metadata_revision_detail"] = history
        staged = stage(runtime, uid, ("record", UUID(record_id)), raw_json(metadata))
    else:
        current = before["metadata_json"]
        legacy_metadata = json.loads(current) if isinstance(current, str) else current or {}
        legacy_history = {"before": legacy_metadata, "title_before": legacy_title, "changes": changes, "reason": payload.reason}
        legacy_metadata = {**legacy_metadata, **changes}
    try:
        with tx() as db:
            row, _ = begin_mutation(db, request, record_id, payload.model_dump_json())
            require_capability(db, uid, record_id, "can_edit_metadata")
            if staged:
                cat.publish_many(db, (staged,))
            elif "title" in changes:
                db.execute(text("UPDATE uploads SET title=:t WHERE id=:r"), {"r": record_id, "t": changes["title"]})
            if not staged:
                db.execute(text("UPDATE records SET metadata_json=:m WHERE id=:r"), {"m": json.dumps(legacy_metadata), "r": record_id})
            semantic = any(field != "title" for field in changes)
            for field, role in (("subject_member_ids", "SUBJECT"), ("participant_member_ids", "PARTICIPANT")):
                if field in changes:
                    db.execute(text("DELETE FROM record_people WHERE record_id=:r AND role=:role"), {"r": record_id, "role": role})
                    for member in set(changes[field] or []):
                        db.execute(text("INSERT INTO record_people(record_id,member_id,role,approved) VALUES (:r,:m,:role,false)"), {"r": record_id, "m": member, "role": role})
            db.execute(text("UPDATE records SET metadata_revision=metadata_revision+1,review_status=CASE WHEN :semantic THEN 'PENDING_REVIEW' ELSE review_status END WHERE id=:r"), {"r": record_id, "semantic": semantic})
            if semantic:
                invalidate_evidence(db, record_id)
            revision = str(uuid4())
            db.execute(text("INSERT INTO record_metadata_revisions(id,record_id,actor_member_id,metadata_revision,resulting_record_version,changed_fields,reason_digest,source_affecting,object_id,object_version,legacy_detail) VALUES (:id,:r,:m,:v,:rv,:fields,:reason,:semantic,:o,:ov,:legacy)"),
                {"id": revision, "r": record_id, "m": row["actor_member_id"], "v": row["metadata_revision"] + 1, "rv": row["record_version"] + 1,
                 "fields": json.dumps(sorted(changes)), "reason": hashlib.sha256(payload.reason.encode()).hexdigest(), "semantic": semantic,
                 "o": str(staged[1].object_id) if staged else None, "ov": staged[1].object_version if staged else None,
                 "legacy": json.dumps(legacy_history) if legacy_history else None})
            finish_mutation(db, request, row, action="METADATA_APPLIED", reason=payload.reason, payload=payload.model_dump_json(), invalidate=semantic)
    except BaseException:
        if staged:
            abandon_staged((staged,))
        raise
    return await record_response(record_id, request)


@router.get("/records/{record_id}/metadata-revisions")
async def metadata_revisions(record_id: str, request: Request):
    from app.api.routes.secure_media import open_bytes
    from app.services import secure_catalogue as cat
    runtime = getattr(request.app.state, "secure_runtime", None)
    with tx() as db:
        record = require_record_access(db, principal(request), record_id)
        owner = record["actor_member_id"] == record["owner_member_id"]
        items = [dict(row) for row in db.execute(text("SELECT * FROM record_metadata_revisions WHERE record_id=:r ORDER BY metadata_revision"), {"r": record_id}).mappings()]
        saved = cat.snapshot(db, principal_id=UUID(principal(request)), targets=(("record", UUID(record_id)),)) if runtime else None
        envelopes = {}
        if owner and runtime:
            for item in items:
                encrypted = db.execute(text("SELECT * FROM encrypted_objects WHERE object_id=:o AND object_version=:v AND record_id=:r AND owner_id=:room AND artifact_kind='record_metadata'"),
                    {"o": item["object_id"], "v": item["object_version"], "r": record_id, "room": record["room_id"]}).mappings().one_or_none()
                if encrypted:
                    envelopes[item["id"]] = cat.envelope_values(encrypted)
    for item in items:
        if isinstance(item["changed_fields"], str):
            item["changed_fields"] = json.loads(item["changed_fields"])
        item["created_at"] = str(item["created_at"])
        if owner and item["id"] in envelopes:
            item["detail"] = json.loads(open_bytes(runtime, envelopes[item["id"]])).get("metadata_revision_detail")
        elif owner and item["legacy_detail"]:
            detail = item["legacy_detail"]
            item["detail"] = json.loads(detail) if isinstance(detail, str) else detail
        for private in ("object_id", "object_version", "legacy_detail", "reason_digest"):
            item.pop(private)
    with tx() as db:
        require_record_access(db, principal(request), record_id)
        if saved:
            cat.revalidate(db, saved)
    return {"items": items, "next_cursor": None}


@router.post("/records/{record_id}/reviews")
async def approve_metadata(record_id: str, payload: RecordAction, request: Request):
    from app.services.record_service_response import record_response
    with tx() as db:
        row, replay = begin_mutation(db, request, record_id, payload.model_dump_json())
        if not replay:
            if row["actor_member_id"] != row["owner_member_id"]:
                raise HTTPException(403, {"code": "ACTION_NOT_ALLOWED"})
            if row["lifecycle_state"] in ("SOFT_DELETED", "PURGED"):
                raise HTTPException(409, {"code": "INVALID_TRANSITION"})
            db.execute(text("UPDATE records SET review_status='APPROVED' WHERE id=:r"), {"r": record_id})
            db.execute(text("UPDATE record_people SET approved=true WHERE record_id=:r"), {"r": record_id})
            finish_mutation(db, request, row, action="METADATA_APPROVED", reason=payload.reason, payload=payload.model_dump_json())
    return await record_response(record_id, request)
