"""auth_identities table, deterministic backfill, users.google_sub nullable

Revision ID: 0002_auth_identities
Revises: 0001_baseline
Create Date: 2026-09-03

Creates ``auth_identities`` with the constraints approved in
docs/backend/decisions/FOUNDATION_IDENTITY_MIGRATION_DECISION.md D1/D2, backfills one
row per existing ``users`` row using the documented mapping, and relaxes
``users.google_sub`` to nullable via batch operations (portable across SQLite and
PostgreSQL). Purely additive plus one nullability relaxation; reversible per D4
only when ``_downgrade_preflight`` reports no refusals (A4).
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_auth_identities"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

LOCAL_PREFIX = "local:"
DOWNGRADE_REFUSAL_CATEGORIES = (
    "dual_identity",
    "zero_identity",
    "empty_subject",
    "username_mismatch",
    "password_hash_mismatch",
    "google_subject_local_prefix",
    "reconstructed_collision",
    "legacy_value_conflict",
)


def _downgrade_preflight(bind) -> dict[str, int]:
    """Count states that cannot round-trip to the legacy identity representation.

    Read-only and category-only: no row values are returned or disclosed.
    """
    users = bind.execute(sa.text("SELECT id, google_sub, username, password_hash FROM users")).all()
    identities = bind.execute(
        sa.text("SELECT user_id, provider, provider_subject, credential_hash FROM auth_identities")
    ).all()
    counts = dict.fromkeys(DOWNGRADE_REFUSAL_CATEGORIES, 0)

    by_user: dict[str, list] = {}
    for row in identities:
        by_user.setdefault(row.user_id, []).append(row)

    reconstructed: dict[str, str] = {}
    for user_id, _google_sub, username, password_hash in users:
        rows = by_user.get(user_id, [])
        if not rows:
            counts["zero_identity"] += 1
            continue
        if len(rows) > 1:
            counts["dual_identity"] += 1
            continue
        _, provider, subject, credential_hash = rows[0]
        if not subject:
            counts["empty_subject"] += 1
            continue
        if provider == "local":
            if not username or username != subject:
                counts["username_mismatch"] += 1
            if not password_hash or password_hash != credential_hash:
                counts["password_hash_mismatch"] += 1
            reconstructed[user_id] = f"{LOCAL_PREFIX}{subject}"
        else:
            if subject.startswith(LOCAL_PREFIX):
                counts["google_subject_local_prefix"] += 1
            reconstructed[user_id] = subject

    values = list(reconstructed.values())
    counts["reconstructed_collision"] = len(values) - len(set(values))

    current_owner = {google_sub: user_id for user_id, google_sub, _, _ in users if google_sub is not None}
    counts["legacy_value_conflict"] = sum(
        1 for user_id, value in reconstructed.items() if current_owner.get(value, user_id) != user_id
    )
    return counts


def upgrade() -> None:
    op.create_table(
        "auth_identities",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_subject", sa.String(), nullable=False),
        sa.Column("credential_hash", sa.String(), nullable=True),
        sa.Column("provider_email", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "provider_subject", name="uq_auth_identity_provider_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_auth_identity_user_provider"),
        sa.CheckConstraint(
            "(provider = 'local') = (credential_hash IS NOT NULL)",
            name="ck_auth_identity_local_requires_hash",
        ),
        sa.CheckConstraint(
            "provider IN ('local', 'google')",
            name="ck_auth_identity_provider_allowed",
        ),
    )
    op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])

    bind = op.get_bind()
    users_table = sa.table(
        "users",
        sa.column("id", sa.String()),
        sa.column("google_sub", sa.String()),
        sa.column("username", sa.String()),
        sa.column("password_hash", sa.String()),
        sa.column("email", sa.String()),
    )
    auth_identities_table = sa.table(
        "auth_identities",
        sa.column("id", sa.String()),
        sa.column("user_id", sa.String()),
        sa.column("provider", sa.String()),
        sa.column("provider_subject", sa.String()),
        sa.column("credential_hash", sa.String()),
        sa.column("provider_email", sa.String()),
    )

    existing_users = bind.execute(
        sa.select(
            users_table.c.id,
            users_table.c.google_sub,
            users_table.c.username,
            users_table.c.password_hash,
            users_table.c.email,
        )
    ).all()

    backfill_rows = []
    for user_id, google_sub, username, password_hash, email in existing_users:
        google_sub = google_sub or ""
        if google_sub.startswith(LOCAL_PREFIX):
            provider = "local"
            provider_subject = username or google_sub[len(LOCAL_PREFIX):]
            credential_hash = password_hash
        else:
            provider = "google"
            provider_subject = google_sub
            credential_hash = None
        backfill_rows.append(
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "provider": provider,
                "provider_subject": provider_subject,
                "credential_hash": credential_hash,
                "provider_email": email,
            }
        )

    if backfill_rows:
        bind.execute(sa.insert(auth_identities_table), backfill_rows)

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("google_sub", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    refusals = {category: count for category, count in _downgrade_preflight(bind).items() if count}
    if refusals:
        raise RuntimeError(
            "0002 downgrade refused: "
            + ", ".join(f"{category}={count}" for category, count in refusals.items())
        )

    users_table = sa.table("users", sa.column("id", sa.String()), sa.column("google_sub", sa.String()))
    auth_identities_table = sa.table(
        "auth_identities",
        sa.column("user_id", sa.String()),
        sa.column("provider", sa.String()),
        sa.column("provider_subject", sa.String()),
    )

    rows = bind.execute(
        sa.select(
            auth_identities_table.c.user_id,
            auth_identities_table.c.provider,
            auth_identities_table.c.provider_subject,
        )
    ).all()
    for user_id, provider, provider_subject in rows:
        value = f"{LOCAL_PREFIX}{provider_subject}" if provider == "local" else provider_subject
        bind.execute(
            sa.update(users_table).where(users_table.c.id == user_id).values(google_sub=value)
        )

    # Drop the referencing table before rebuilding users (SQLite batch mode).
    op.drop_index("ix_auth_identities_user_id", table_name="auth_identities")
    op.drop_table("auth_identities")

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("google_sub", existing_type=sa.String(), nullable=False)
