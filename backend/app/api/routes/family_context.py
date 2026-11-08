"""Additive M3 read-only projection and deterministic context endpoints."""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.family_context import (
    ContextFilters,
    EventStatus,
    ParticipationMode,
    QueryContext,
    Resolution,
    ResolutionRequest,
)
from app.services import family_context as service

router = APIRouter(prefix="/families")


@router.post("/{room_id}/relation-resolutions", response_model=Resolution)
def relation_resolution(room_id: str, payload: ResolutionRequest, *, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    return service.resolve(db, user.id, room_id, payload.surface, payload.as_of)


@router.get("/{room_id}/context-relations")
def context_relations(room_id: str, as_of: date | None = None, *, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    return service.relations(db, user.id, room_id, as_of)


@router.get("/{room_id}/events")
def family_events(room_id: str, participation: ParticipationMode | None = None, person_id: str | None = None,
                  event_status: EventStatus = "COMPLETED", as_of: date | None = None,
                  *, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    return service.events(db, user.id, room_id, mode=participation, person_id=person_id, status=event_status, as_of=as_of)


@router.post("/{room_id}/query-context", response_model=QueryContext)
def structured_context(room_id: str, payload: ContextFilters, *, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    return service.query_context(db, user.id, room_id, payload)
