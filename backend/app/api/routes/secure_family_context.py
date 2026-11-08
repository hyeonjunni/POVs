"""M3 SECURE endpoints; same additive contracts, no plaintext persistence."""
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from app.ai.grounded_answering import GroundedEvidenceChanged
from app.api.routes.secure_operations import principal
from app.schemas.claims import ClaimReview, ClaimSubmission
from app.schemas.family_context import (
    ContextFilters,
    EventStatus,
    ParticipationMode,
    QueryContext,
    Resolution,
    ResolutionRequest,
)
from app.services.secure_family_context import query
from app.schemas.personal_context import PersonalContextUpdate
from app.services import personal_context

router = APIRouter(prefix="/families")


@router.get("/{room_id}/personal-context")
async def read_personal_context(room_id: str, request: Request):
    return personal_context.read(request.app.state.secure_runtime, principal(request), room_id)


@router.put("/{room_id}/personal-context")
async def save_personal_context(room_id: str, payload: PersonalContextUpdate, request: Request):
    return personal_context.save(request.app.state.secure_runtime, principal(request), room_id, payload)


@router.post("/{room_id}/relation-resolutions", response_model=Resolution)
async def relation_resolution(room_id: str, payload: ResolutionRequest, request: Request):
    return query(request.app.state.secure_runtime, principal(request), room_id, "resolve", payload.surface, payload.as_of)


@router.get("/{room_id}/context-relations")
async def context_relations(room_id: str, request: Request, as_of: date | None = None):
    return query(request.app.state.secure_runtime, principal(request), room_id, "relations", as_of)


@router.get("/{room_id}/events")
async def family_events(room_id: str, request: Request, participation: ParticipationMode | None = None,
    person_id: str | None = None, event_status: EventStatus = "COMPLETED", as_of: date | None = None):
    return query(request.app.state.secure_runtime, principal(request), room_id, "events", mode=participation,
        person_id=person_id, status=event_status, as_of=as_of)


@router.post("/{room_id}/query-context", response_model=QueryContext)
async def structured_context(room_id: str, payload: ContextFilters, request: Request):
    return query(request.app.state.secure_runtime, principal(request), room_id, "query_context", payload)


@router.post("/{room_id}/claims")
async def submit_claim(room_id: str, payload: ClaimSubmission, request: Request):
    from app.services.claims import submit
    try:
        return submit(request.app.state.secure_runtime, principal(request), room_id, payload.payload, payload.status)
    except GroundedEvidenceChanged:
        raise HTTPException(409, "STALE_SOURCE") from None
    except ValueError:
        raise HTTPException(400, "Invalid claim submission") from None


@router.post("/{room_id}/claims/{claim_id}/review")
async def review_claim(room_id: str, claim_id: str, payload: ClaimReview, request: Request):
    from app.services.claims import review
    try:
        return review(request.app.state.secure_runtime, principal(request), room_id, claim_id, payload.claim_version, payload.status)
    except GroundedEvidenceChanged:
        raise HTTPException(409, "STALE_SOURCE") from None
    except ValueError:
        raise HTTPException(400, "Invalid claim review") from None
