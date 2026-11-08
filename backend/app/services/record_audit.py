"""Persist neutral access-denial events after the rejected transaction rolls back."""
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core import database
from app.services.record_policy import RecordAccessDenied


async def denied_record_response(request: Request, error: Exception) -> JSONResponse:
    """Unknown and unauthorized targets have the same response and audit shape."""
    if not isinstance(error, RecordAccessDenied):
        raise error
    try:
        record_id = str(UUID(error.record_id))
    except ValueError:
        record_id = "invalid-target"
    with database.engine.begin() as db:
        db.execute(text(
            "INSERT INTO record_audits "
            "(id,record_id,actor_user_id,action,before_version,after_version,request_id) "
            "VALUES (:id,:record,:actor,'ACCESS_DENIED',0,0,:request)"
        ), {"id": str(uuid4()), "record": record_id,
            "actor": error.actor_user_id, "request": str(uuid4())})
    return JSONResponse({"detail": {"code": "RESOURCE_NOT_AVAILABLE"}}, status_code=404)
