from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class AuthIdentity(Base):
    """A login method for a `users` row (FOUNDATION_IDENTITY_MIGRATION_DECISION.md D1/D2).

    `users.id` stays the sole internal identifier and JWT subject; this table
    only records how a person authenticates.
    """

    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_subject", name="uq_auth_identity_provider_subject"),
        UniqueConstraint("user_id", "provider", name="uq_auth_identity_user_provider"),
        CheckConstraint(
            "((payload_state='LEGACY' AND ((provider='local')=(credential_hash IS NOT NULL))) OR (payload_state='ENCRYPTED' AND provider_subject IS NULL AND provider_email IS NULL AND credential_hash IS NULL))",
            name="ck_auth_identity_local_requires_hash",
        ),
        CheckConstraint("provider IN ('local', 'google')", name="ck_auth_identity_provider_allowed"),
        CheckConstraint("payload_state IN ('LEGACY','ENCRYPTED')", name="ck_auth_identity_payload_state"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_subject: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="LEGACY")
    credential_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def load_local_identity(db: Session, username: str) -> tuple[User, AuthIdentity] | None:
    """Resolve a local login by `(provider="local", provider_subject=username)`.

    Never infers identity from email; returns `None` if no such identity exists.
    """
    from app.models.user import User

    identity = db.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == "local",
            AuthIdentity.provider_subject == username,
        )
    )
    if identity is None:
        return None

    user = db.get(User, identity.user_id)
    if user is None:
        return None

    return user, identity


class PrincipalNotPersisted(Exception):
    """JWT subject has no `users` row or no `auth_identities` row (A6). Not a
    LookupError on purpose: callers map LookupError to 404, this must be 401."""


def authenticated_identity_matches(payload, *, user_id, identity_id, provider, normalized_identifier):
    """Called only after authenticated decryption, including verified provider claims.

    Email is deliberately not an identity proof or an account linking mechanism.
    """
    return (type(payload) is dict and payload.get("user_id") == user_id
            and payload.get("identity_id") == identity_id and payload.get("provider") == provider
            and type(payload.get("normalization_version")) is int and payload["normalization_version"] == 1
            and payload.get("normalized_identifier") == normalized_identifier)


def require_persisted_principal(db: Session, user_id: str) -> User:
    """The one persisted User/AuthIdentity check every token-only path runs
    inside its Transaction A, before any ACL or data read (A6)."""
    from app.models.user import User

    user = db.get(User, user_id)
    if user is None:
        raise PrincipalNotPersisted("Authenticated user not found")
    has_identity = db.scalar(select(AuthIdentity.id).where(AuthIdentity.user_id == user.id).limit(1))
    if has_identity is None:
        raise PrincipalNotPersisted("Authenticated user not found")
    return user
