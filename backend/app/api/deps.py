from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.auth_identity import PrincipalNotPersisted, require_persisted_principal
from app.models.user import User


bearer_scheme = HTTPBearer(auto_error=False)


def _resolve_bearer_token(
    credentials: HTTPAuthorizationCredentials | None
) -> str:
    token = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(status_code=401, detail="Authentication credentials were not provided")
    return token


def _decode_subject(token: str) -> str:
    try:
        payload = decode_access_token(token)
    except Exception as error:
        raise HTTPException(status_code=401, detail="Invalid or expired access token") from error

    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid access token payload")
    return user_id


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    token = _resolve_bearer_token(credentials)
    user_id = _decode_subject(token)

    try:
        return require_persisted_principal(db, user_id)
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Token-only identity: no database session is opened to produce this.

    Used by slow-I/O routes (e.g. chat-demo) that must not hold a DB session
    while a storage/OCR/STT/model call runs (D5). CRUD routes keep using
    `get_current_user`.
    """

    user_id: str


def get_authenticated_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedPrincipal:
    token = _resolve_bearer_token(credentials)
    user_id = _decode_subject(token)
    return AuthenticatedPrincipal(user_id=user_id)
