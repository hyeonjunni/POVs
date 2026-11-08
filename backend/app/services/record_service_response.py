from dataclasses import asdict

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes.secure_operations import principal, tx
from app.services.record_lifecycle import utc_datetime
from app.services.record_policy import record_capabilities, require_record_access


async def record_response(record_id: str, request: Request):
    uid = principal(request)
    with tx() as db:
        row = dict(require_record_access(db, uid, record_id, purpose="manage"))
        caps = asdict(record_capabilities(db, uid, record_id))
        people = [dict(p) for p in db.execute(text("SELECT member_id,role,approved FROM record_people WHERE record_id=:r"), {"r": record_id}).mappings()]
        grants = list(db.execute(text("SELECT member_id FROM record_grants WHERE record_id=:r"), {"r": record_id}).scalars())
        restrictions = [dict(item) for item in db.execute(text("SELECT id,member_id,active FROM record_restrictions WHERE record_id=:r AND (member_id=:m OR :m=:owner)"), {"r": record_id, "m": row["actor_member_id"], "owner": row["owner_member_id"]}).mappings()]
    fields = ("room_id", "owner_member_id", "uploader_member_id", "visibility", "lifecycle_state", "review_status", "record_version", "metadata_revision", "source_revision", "authorization_epoch", "purge_status")
    result = {key: row[key] for key in fields}
    result.update(record_id=record_id, capabilities=caps, people=people, custom_grants=grants, restrictions=restrictions)
    if row["soft_deleted_at"]:
        from datetime import timedelta
        result["restore_deadline"] = (utc_datetime(row["soft_deleted_at"]) + timedelta(days=30)).isoformat()
    if caps["can_read"]:
        if getattr(request.app.state, "secure_runtime", None) is not None:
            from app.api.routes.secure_media import record_payload, record_source
            from app.services import secure_catalogue as cat
            with tx() as db:
                before = record_source(db, uid, record_id)
            payload = record_payload(request.app.state.secure_runtime, before)
            with tx() as db:
                cat.revalidate(db, before[0])
            result.update(title=payload["title"], description=payload.get("description"), tags=payload.get("tags", []), metadata=payload.get("m2_metadata", {}))
        else:
            import json
            with tx() as db:
                require_record_access(db, uid, record_id)
                upload = db.execute(text("SELECT title,description FROM uploads WHERE id=:r"), {"r": record_id}).mappings().one()
                result.update(title=upload["title"], description=upload["description"])
                stored = row["metadata_json"]
                result["metadata"] = json.loads(stored) if isinstance(stored, str) else stored or {}
    request.scope["record_response_version"] = row["record_version"]
    return JSONResponse(result, headers={"ETag": f'"{row["record_version"]}"'})
