"""Alembic adoption for fresh, legacy, and revision-stamped databases.

Implements FOUNDATION_IDENTITY_MIGRATION_DECISION.md D3 and
FOUNDATION_AUDIT_1_AMENDMENT.md A2/A3: fail-closed revision verification,
canonical related-user FK normalization without rewriting rows, and FK-checked
transactional SQLite migration DDL.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import BigInteger, Boolean, CHAR, Connection, Date, DateTime, Integer, String, Text, column, create_engine, event, func, inspect, select, table
from sqlalchemy.pool import NullPool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_LOCATION = BACKEND_ROOT / "alembic"

BASELINE_TABLES = ("users", "family_rooms", "family_members", "uploads", "upload_files")

BASELINE_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": (
        "id", "google_sub", "username", "password_hash", "email", "name",
        "profile_image", "age", "gender", "phone", "profile_chunk", "created_at",
    ),
    "family_rooms": ("id", "name", "invite_code", "owner_user_id", "created_at"),
    "family_members": (
        "id", "room_id", "user_id", "role", "related_to_user_id",
        "relation_to_related_user", "joined_at",
    ),
    "uploads": (
        "id", "room_id", "uploader_user_id", "type", "title", "description",
        "status", "created_at",
    ),
    "upload_files": (
        "id", "upload_id", "storage_bucket", "storage_path", "mime_type",
        "file_size", "encrypted", "created_at",
    ),
}

NOT_NULL_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("id", "google_sub", "email", "name", "created_at"),
    "family_rooms": ("id", "name", "invite_code", "owner_user_id", "created_at"),
    "family_members": ("id", "room_id", "user_id", "role", "joined_at"),
    "uploads": ("id", "room_id", "uploader_user_id", "type", "title", "status", "created_at"),
    "upload_files": (
        "id", "upload_id", "storage_bucket", "storage_path", "mime_type", "encrypted", "created_at",
    ),
}

# PF-3: unique indexes/constraints expected on a fully-migrated legacy database.
EXPECTED_UNIQUE_COLUMNS: dict[str, tuple[tuple[str, ...], ...]] = {
    "users": (("google_sub",), ("email",), ("username",)),
    "family_rooms": (("invite_code",),),
    "family_members": (("room_id", "user_id"),),
}

REVISION_CHAIN = (
    "0001_baseline",
    "0002_auth_identities",
    "0003_processing_jobs",
    "0004_retrieval",
    "0005_secure_catalogue",
    "0006_family_graph",
    "0008_upload_file_uploader",
    "0009_ocr_review",
)
REVISION_PARENTS: dict[str, tuple[str, ...]] = {
    revision: (() if index == 0 else (REVISION_CHAIN[index - 1],))
    for index, revision in enumerate(REVISION_CHAIN)
}
ALBUM_MERGE_REVISION = "0010_merge_album_family"
QUOTA_REVISION = "0012_secure_room_storage_quota"
RECONCILIATION_REVISION = "0013_secure_reconciliation"
MERGED_HEAD = "0016_family_context"
REVISION_PARENTS.update({
    "0006_photo_albums": ("0005_secure_catalogue",),
    "0007_video_albums_reads": ("0006_photo_albums",),
    ALBUM_MERGE_REVISION: ("0007_video_albums_reads", "0009_ocr_review"),
    "0011_legacy_room_storage_quota": (ALBUM_MERGE_REVISION,),
    QUOTA_REVISION: ("0011_legacy_room_storage_quota",),
    RECONCILIATION_REVISION: (QUOTA_REVISION,),
    "0014_native_text": (RECONCILIATION_REVISION,),
    "0015_record_privacy": ("0014_native_text",),
    MERGED_HEAD: ("0015_record_privacy",),
})
REVISION_CHAIN = tuple(REVISION_PARENTS)
KNOWN_REVISIONS = set(REVISION_PARENTS)


def _ancestors(revision: str) -> set[str]:
    if revision not in KNOWN_REVISIONS:
        raise RuntimeError(f"Unrecognized revision {revision!r}")
    ancestors = {revision}
    for parent in REVISION_PARENTS[revision]:
        ancestors.update(_ancestors(parent))
    return ancestors


def _frontier_ancestors(revisions) -> set[str]:
    revisions = tuple(revisions)
    if not revisions or len(set(revisions)) != len(revisions):
        raise RuntimeError("Revision frontier must be nonempty and unique")
    ancestry = {revision: _ancestors(revision) for revision in revisions}
    if any(left != right and left in ancestry[right]
           for left in revisions for right in revisions):
        raise RuntimeError("Revision frontier contains redundant ancestors")
    return set().union(*ancestry.values())


AUTH_IDENTITY_CONSTRAINTS = (
    "uq_auth_identity_provider_subject", "uq_auth_identity_user_provider",
    "ck_auth_identity_local_requires_hash", "ck_auth_identity_provider_allowed",
)
PROCESSING_JOB_CONSTRAINTS = (
    "uq_processing_job_record_file_processor", "ck_processing_job_processor_allowed", "ck_processing_job_status_allowed",
)
DELETION_INTENT_CONSTRAINTS = ("ck_deletion_intent_scope_allowed", "ck_deletion_intent_status_allowed")

AUTH_IDENTITY_COLUMNS = {
    "id": (String, False, None),
    "user_id": (String, False, None),
    "provider": (String, False, None),
    "provider_subject": (String, False, None),
    "credential_hash": (String, True, None),
    "provider_email": (String, True, None),
    "created_at": (DateTime, False, "now"),
}
PROCESSING_JOB_COLUMNS = {
    "id": (String, False, None),
    "record_id": (String, False, None),
    "upload_file_id": (String, False, None),
    "room_id": (String, False, None),
    "processor": (String, False, None),
    "status": (String, False, "pending"),
    "attempt": (Integer, False, "0"),
    "result_json": (Text, True, None),
    "failure_code": (String, True, None),
    "failure_message": (String, True, None),
    "started_at": (DateTime, True, None),
    "completed_at": (DateTime, True, None),
    "created_at": (DateTime, False, "now"),
    "updated_at": (DateTime, False, "now"),
}
DELETION_INTENT_COLUMNS = {
    "id": (String, False, None),
    "scope": (String, False, None),
    "scope_id": (String, False, None),
    "storage_bucket": (String, False, None),
    "storage_path": (String, False, None),
    "status": (String, False, None),
    "failure_code": (String, True, None),
    "created_at": (DateTime, False, "now"),
    "updated_at": (DateTime, False, "now"),
}
STORAGE_ALLOCATION_COLUMNS = {
    "id": (String, False, None),
    "room_id": (String, False, None),
    "upload_id": (String, False, None),
    "storage_bucket": (String, False, None),
    "storage_path": (String, False, None),
    "byte_size": (BigInteger, False, None),
    "state": (String, False, None),
    "reserved_at": (DateTime, False, "now"),
    "expires_at": (DateTime, False, None),
    "updated_at": (DateTime, False, "now"),
}
RECORD_CHUNK_COLUMNS = {
    "id": (String, False, None),
    "room_id": (String, False, None),
    "record_id": (String, False, None),
    "file_id": (String, False, None),
    "processing_job_id": (String, False, None),
    "source_attempt": (Integer, False, None),
    "source_result_sha256": (lambda: CHAR(64), False, None),
    "ordinal": (Integer, False, None),
    "text": (Text, False, None),
    "text_sha256": (lambda: CHAR(64), False, None),
    "token_count": (Integer, False, None),
    "char_start": (Integer, False, None),
    "char_end": (Integer, False, None),
    "source_locator_json": (Text, False, None),
    "extractor": (String, False, None),
    "extractor_id": (String, False, None),
    "review_status": (String, False, None),
    "user_confirmed": (Boolean, False, None),
    "chunker_version": (String, False, None),
    "created_at": (DateTime, False, "now"),
    "updated_at": (DateTime, False, "now"),
}
RECORD_EMBEDDING_COLUMNS = {
    "id": (String, False, None),
    "chunk_id": (String, False, None),
    "model_key": (String, False, None),
    "dimensions": (Integer, False, None),
    "embedding": ("embedding", False, None),
    "created_at": (DateTime, False, "now"),
}

FAMILY_PEOPLE_COLUMNS = {
    "id": (String, False, None),
    "room_id": (String, False, None),
    "user_id": (String, True, None),
    "display_name": (String, False, None),
    "gender": (String, True, None),
    "birth_date": (Date, True, None),
    "death_date": (Date, True, None),
    "created_at": (DateTime, False, "now"),
    "updated_at": (DateTime, False, "now"),
}
FAMILY_RELATIONSHIP_COLUMNS = {
    "id": (String, False, None),
    "room_id": (String, False, None),
    "from_person_id": (String, False, None),
    "to_person_id": (String, False, None),
    "relation_type": (String, False, None),
    "created_at": (DateTime, False, "now"),
    "updated_at": (DateTime, False, "now"),
}

REVISION_TABLE_DEFINITIONS = {
    "legacy_storage_allocations": {
        "columns": STORAGE_ALLOCATION_COLUMNS,
        "indexes": {
            "ix_legacy_allocation_room_state": (("room_id", "state"), False),
            "ix_legacy_allocation_upload_state": (("upload_id", "state"), False),
        },
        "unique": {
            "uq_legacy_allocation_locator": ("storage_bucket", "storage_path"),
        },
        "foreign_keys": set(),
        "checks": {
            "ck_legacy_allocation_local_only": "storage_bucket='local-media'",
            "ck_legacy_allocation_positive_size": "byte_size>0",
            "ck_legacy_allocation_state": (
                "state in ('reserved','active','delete_pending','released')"
            ),
        },
    },
    "family_people": {
        "columns": FAMILY_PEOPLE_COLUMNS,
        "indexes": {},
        "unique": {
            "uq_family_people_room_id": ("room_id", "id"),
            "uq_family_people_room_user": ("room_id", "user_id"),
        },
        "foreign_keys": {
            (("room_id",), "family_rooms", ("id",)),
            (("user_id",), "users", ("id",)),
        },
        "foreign_key_actions": {
            (("room_id",), "family_rooms", ("id",)): "CASCADE",
            (("user_id",), "users", ("id",)): "SET NULL",
        },
        "cascade_foreign_keys": {(("room_id",), "family_rooms", ("id",))},
        "checks": {},
    },
    "family_relationships": {
        "columns": FAMILY_RELATIONSHIP_COLUMNS,
        "indexes": {},
        "unique": {
            "uq_family_relationship_edge": ("room_id", "from_person_id", "to_person_id", "relation_type"),
        },
        "foreign_keys": {
            (("room_id",), "family_rooms", ("id",)),
            (("room_id", "from_person_id"), "family_people", ("room_id", "id")),
            (("room_id", "to_person_id"), "family_people", ("room_id", "id")),
        },
        "foreign_key_actions": {
            (("room_id",), "family_rooms", ("id",)): "CASCADE",
            (("room_id", "from_person_id"), "family_people", ("room_id", "id")): "CASCADE",
            (("room_id", "to_person_id"), "family_people", ("room_id", "id")): "CASCADE",
        },
        "cascade_foreign_keys": {
            (("room_id",), "family_rooms", ("id",)),
            (("room_id", "from_person_id"), "family_people", ("room_id", "id")),
            (("room_id", "to_person_id"), "family_people", ("room_id", "id")),
        },
        "checks": {
            "ck_family_relationship_not_self": "from_person_id != to_person_id",
            "ck_family_relationship_type": "relation_type in ('parent_of', 'spouse_of')",
            "ck_family_relationship_spouse_order": "relation_type != 'spouse_of' or from_person_id < to_person_id",
        },
    },
    "auth_identities": {
        "columns": AUTH_IDENTITY_COLUMNS,
        "indexes": {"ix_auth_identities_user_id": (("user_id",), False)},
        "unique": {
            "uq_auth_identity_provider_subject": ("provider", "provider_subject"),
            "uq_auth_identity_user_provider": ("user_id", "provider"),
        },
        "foreign_keys": {(("user_id",), "users", ("id",))},
        "checks": {
            "ck_auth_identity_local_requires_hash": "(provider = 'local') = (credential_hash is not null)",
            "ck_auth_identity_provider_allowed": "provider in ('local', 'google')",
        },
    },
    "processing_jobs": {
        "columns": PROCESSING_JOB_COLUMNS,
        "indexes": {
            "ix_processing_jobs_record_id": (("record_id",), False),
            "ix_processing_jobs_room_id": (("room_id",), False),
        },
        "unique": {
            "uq_processing_job_record_file_processor": ("record_id", "upload_file_id", "processor"),
        },
        "foreign_keys": {
            (("record_id",), "uploads", ("id",)),
            (("upload_file_id",), "upload_files", ("id",)),
            (("room_id",), "family_rooms", ("id",)),
        },
        "checks": {
            "ck_processing_job_processor_allowed": "processor in ('ocr', 'stt')",
            "ck_processing_job_status_allowed": "status in ('pending', 'running', 'succeeded', 'failed')",
        },
    },
    "deletion_intents": {
        "columns": DELETION_INTENT_COLUMNS,
        "indexes": {"ix_deletion_intents_status": (("status",), False)},
        "unique": {},
        "foreign_keys": set(),
        "checks": {
            "ck_deletion_intent_scope_allowed": "scope in ('record', 'room', 'account')",
            "ck_deletion_intent_status_allowed": "status in ('pending', 'deleted', 'absent', 'failed')",
        },
    },
    "record_chunks": {
        "columns": RECORD_CHUNK_COLUMNS,
        "indexes": {
            "ix_record_chunks_room_id_id": (("room_id", "id"), False),
            "ix_record_chunks_record_id": (("record_id",), False),
            "ix_record_chunks_file_id": (("file_id",), False),
        },
        "unique": {
            "uq_record_chunk_source_version_ordinal": (
                "processing_job_id",
                "source_attempt",
                "chunker_version",
                "ordinal",
            ),
        },
        "foreign_keys": {
            (("record_id", "room_id"), "uploads", ("id", "room_id")),
            (("file_id", "record_id"), "upload_files", ("id", "upload_id")),
            (
                ("processing_job_id", "record_id", "file_id", "room_id"),
                "processing_jobs",
                ("id", "record_id", "upload_file_id", "room_id"),
            ),
        },
        "cascade_foreign_keys": {
            (
                ("processing_job_id", "record_id", "file_id", "room_id"),
                "processing_jobs",
                ("id", "record_id", "upload_file_id", "room_id"),
            ),
        },
        "checks": {
            "ck_record_chunk_ordinal_nonnegative": "ordinal >= 0",
            "ck_record_chunk_token_count_nonnegative": "token_count >= 0",
            "ck_record_chunk_char_start_nonnegative": "char_start >= 0",
            "ck_record_chunk_char_end_order": "char_end >= char_start",
            "ck_record_chunk_review_status_allowed": (
                "review_status in ('unreviewed', 'confirmed', 'rejected')"
            ),
        },
    },
    "record_embeddings": {
        "columns": RECORD_EMBEDDING_COLUMNS,
        "indexes": {
            "ix_record_embeddings_model_key_chunk_id": (("model_key", "chunk_id"), False),
        },
        "unique": {
            "uq_record_embedding_chunk_model": ("chunk_id", "model_key"),
        },
        "foreign_keys": {(("chunk_id",), "record_chunks", ("id",))},
        "cascade_foreign_keys": {(("chunk_id",), "record_chunks", ("id",))},
        "checks": {"ck_record_embedding_dimensions_m2": "dimensions = 1024"},
    },
}

PARENT_COMPOSITE_UNIQUES = {
    "uploads": {"uq_uploads_id_room_id": ("id", "room_id")},
    "upload_files": {"uq_upload_files_id_upload_id": ("id", "upload_id")},
    "processing_jobs": {
        "uq_processing_jobs_id_record_file_room": (
            "id",
            "record_id",
            "upload_file_id",
            "room_id",
        )
    },
}


def migration_engine(database_url: str):
    """Engine for every Alembic run (env.py) and for FK normalization.

    SQLite: the official ALTER TABLE procedure — FK enforcement OFF on the
    connection, one explicit transaction around the DDL (pysqlite in autocommit
    mode, BEGIN issued on SQLAlchemy's begin event), PRAGMA foreign_key_check
    before COMMIT (see migration_transaction)."""
    engine = create_engine(database_url, poolclass=NullPool)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_connection, _record):
            dbapi_connection.isolation_level = None
            dbapi_connection.execute("PRAGMA foreign_keys=OFF")

        @event.listens_for(engine, "begin")
        def _sqlite_begin(connection):
            connection.exec_driver_sql("BEGIN")

    return engine


@contextmanager
def migration_transaction(connection: Connection):
    """One transaction for DDL/DML. On SQLite, referential integrity is verified
    with PRAGMA foreign_key_check before commit; violations roll back."""
    with connection.begin():
        yield
        if connection.dialect.name == "sqlite":
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
            if violations:
                tables = sorted({row[0] for row in violations})
                raise RuntimeError(f"foreign_key_check reported violations in {tables}; rolled back")


def _existing_unique_column_sets(inspector, table_name: str) -> list[frozenset[str]]:
    sets: list[frozenset[str]] = []
    for index in inspector.get_indexes(table_name):
        if index.get("unique"):
            sets.append(frozenset(index["column_names"]))
    for constraint in inspector.get_unique_constraints(table_name):
        sets.append(frozenset(constraint["column_names"]))
    return sets


def preflight(connection: Connection, *, nullable_google_sub: bool = False, secure_catalogue: bool = False) -> list[str]:
    """Return every PF-1..PF-5 failure for an existing database (empty == pass).

    A completely empty database (no tables, no ``alembic_version``) is not a
    failure: it takes the fresh ``alembic upgrade head`` path with nothing to
    check. Never mutates the database and never inspects or prints row values.
    """
    failures: list[str] = []
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())

    if not existing_tables:
        return failures

    # PF-1
    for name in BASELINE_TABLES:
        if name not in existing_tables:
            failures.append(f"PF-1: baseline table '{name}' is missing")

    present_tables = [name for name in BASELINE_TABLES if name in existing_tables]

    # PF-2
    for table_name in present_tables:
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        for column_name in BASELINE_COLUMNS[table_name]:
            if column_name not in existing_columns:
                failures.append(f"PF-2: column '{table_name}.{column_name}' is missing")

    # PF-3
    for table_name, expected_sets in EXPECTED_UNIQUE_COLUMNS.items():
        if table_name not in present_tables:
            continue
        observed_sets = _existing_unique_column_sets(inspector, table_name)
        for expected in expected_sets:
            if frozenset(expected) not in observed_sets:
                failures.append(
                    f"PF-3: unique constraint on {table_name}({', '.join(expected)}) is missing"
                )

    # PF-4
    if "alembic_version" in existing_tables:
        version_table = table("alembic_version", column("version_num", String()))
        stamped_versions = {
            row[0] for row in connection.execute(select(version_table.c.version_num)).all()
        }
        if stamped_versions and not (stamped_versions & KNOWN_REVISIONS):
            failures.append(
                "PF-4: alembic_version is stamped at an unrecognized revision "
                f"({sorted(stamped_versions)})"
            )

    # PF-5
    for table_name in present_tables:
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        for column_name in NOT_NULL_COLUMNS.get(table_name, ()):
            if secure_catalogue and column_name in {
                "users": ("email", "name"), "family_rooms": ("name", "invite_code"),
                "uploads": ("title",), "upload_files": ("storage_bucket", "storage_path", "mime_type"),
            }.get(table_name, ()):
                continue
            if nullable_google_sub and (table_name, column_name) == ("users", "google_sub"):
                continue
            if column_name not in existing_columns:
                continue
            table_ref = table(table_name, column(column_name))
            found_null = connection.execute(
                select(table_ref.c[column_name]).where(table_ref.c[column_name].is_(None)).limit(1)
            ).first()
            if found_null is not None:
                failures.append(f"PF-5: column '{table_name}.{column_name}' contains NULLs")

    return failures


def recorded_revisions(connection: Connection) -> list[str] | None:
    inspector = inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        return None
    version_table = table("alembic_version", column("version_num", String()))
    return sorted(row[0] for row in connection.execute(select(version_table.c.version_num)).all())


def _normalize_default(value) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).replace("(", "").replace(")", "").split()).lower()
    normalized = normalized.split("::", 1)[0].strip("'\"")
    return "now" if normalized == "current_timestamp" else normalized


def _normalize_check(value) -> str:
    return " ".join(str(value).split()).lower()


def _canonical_type_signature(value) -> tuple:
    if isinstance(value, Text):
        return ("text",)
    if isinstance(value, Boolean):
        return ("boolean",)
    if isinstance(value, CHAR):
        return ("char", value.length)
    if isinstance(value, String):
        return ("string", value.length)
    if isinstance(value, BigInteger):
        return ("bigint",)
    if isinstance(value, Integer):
        return ("integer",)
    if isinstance(value, DateTime):
        return ("datetime",)
    if isinstance(value, Date):
        return ("date",)
    return (type(value).__module__, type(value).__qualname__, str(value))


def _verify_table_definition(
    inspector, table_name: str, *, extra_unique: dict[str, tuple[str, ...]] | None = None,
    secure_catalogue: bool = False, ocr_review: bool = False,
    storage_quota: bool = False, native_text: bool = False,
) -> list[str]:
    expected = REVISION_TABLE_DEFINITIONS[table_name]
    if secure_catalogue and table_name == "auth_identities":
        migration = _secure_migration()
        expected = expected | {
            "columns": expected["columns"] | {"provider_subject": (String, True, None), "payload_state": (Text, False, "legacy")},
            "checks": expected["checks"] | {
                "ck_auth_identity_local_requires_hash": _normalize_check(migration.AUTH_PAYLOAD_CHECK),
                "ck_auth_identity_payload_state": "payload_state in ('legacy','encrypted')",
            },
        }
    if storage_quota and table_name == "deletion_intents":
        expected = expected | {
            "columns": expected["columns"] | {
                "storage_allocation_id": (String, True, None),
            },
            "unique": expected["unique"] | {
                "uq_deletion_intent_allocation": ("storage_allocation_id",),
            },
            "foreign_keys": expected["foreign_keys"] | {
                (
                    ("storage_allocation_id",),
                    "legacy_storage_allocations",
                    ("id",),
                ),
            },
        }
    failures: list[str] = []
    actual_columns = {c["name"]: c for c in inspector.get_columns(table_name)}
    expected_columns = expected["columns"] | ({"review_json": (Text, True, None)} if ocr_review else {})
    if set(actual_columns) != set(expected_columns):
        failures.append(
            f"table '{table_name}' columns differ: expected {sorted(expected_columns)}, found {sorted(actual_columns)}"
        )
    for name in set(actual_columns) & set(expected_columns):
        expected_type, expected_nullable, expected_default = expected_columns[name]
        actual = actual_columns[name]
        if expected_type == "embedding":
            dialect_name = inspector.bind.dialect.name
            actual_signature = _canonical_type_signature(actual["type"])
            expected_signature = (
                ("text",)
                if dialect_name == "sqlite"
                else ("vector", 1024)
            )
            if dialect_name == "postgresql":
                dimension = getattr(actual["type"], "dim", None)
                actual_signature = ("vector", dimension)
            if actual_signature != expected_signature:
                failures.append(
                    f"column '{table_name}.{name}' has unexpected {dialect_name} type {actual['type']}"
                )
        elif _canonical_type_signature(actual["type"]) != _canonical_type_signature(expected_type()):
            failures.append(f"column '{table_name}.{name}' has unexpected type {actual['type']}")
        if bool(actual["nullable"]) != expected_nullable:
            failures.append(f"column '{table_name}.{name}' has unexpected nullability")
        if _normalize_default(actual.get("default")) != expected_default:
            failures.append(f"column '{table_name}.{name}' has unexpected server default")

    primary_key = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
    if primary_key != ("id",):
        failures.append(f"table '{table_name}' primary key differs: found {primary_key}")

    indexes = {
        item["name"]: (tuple(item["column_names"]), bool(item.get("unique")))
        for item in inspector.get_indexes(table_name)
        if not item.get("duplicates_constraint")
    }
    if indexes != expected["indexes"]:
        failures.append(f"table '{table_name}' indexes differ")

    unique = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints(table_name)
    }
    expected_unique = expected["unique"] | (extra_unique or {})
    if unique != expected_unique:
        failures.append(f"table '{table_name}' unique constraints differ")

    foreign_key_items = inspector.get_foreign_keys(table_name)
    foreign_keys = {
        (tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
        for item in foreign_key_items
    }
    if foreign_keys != expected["foreign_keys"]:
        failures.append(f"table '{table_name}' foreign keys differ")
    expected_actions = expected.get("foreign_key_actions")
    if expected_actions is not None:
        actual_actions = {
            (tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"])):
                (item.get("options") or {}).get("ondelete", "").upper() or None
            for item in foreign_key_items
        }
        if actual_actions != expected_actions:
            failures.append(f"table '{table_name}' foreign key actions differ")
    cascades = {
        (tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
        for item in foreign_key_items
        if (item.get("options") or {}).get("ondelete", "").upper() == "CASCADE"
    }
    if cascades != expected.get("cascade_foreign_keys", set()):
        failures.append(f"table '{table_name}' cascading foreign keys differ")

    checks = {
        item["name"]: _normalize_check(item["sqltext"])
        for item in inspector.get_check_constraints(table_name)
    }
    expected_checks = expected["checks"]
    if native_text and table_name == "processing_jobs":
        expected_checks = {
            "ck_processing_job_processor_allowed": "processor in ('ocr', 'stt', 'text')",
            "ck_processing_job_status_allowed": "status in ('pending', 'running', 'succeeded', 'failed')",
        }
    if checks != expected_checks:
        failures.append(f"table '{table_name}' CHECK constraints differ")
    return failures


def verify_revision(connection: Connection, revision: str) -> list[str]:
    """Verify the exact schema represented by one revision and its ancestors."""
    return verify_frontier(connection, (revision,))


def verify_migration_boundary(connection: Connection, required_revision: str) -> list[str]:
    """Verify recorded sibling schemas as well as this migration's prerequisite."""
    recorded = recorded_revisions(connection) or ()
    applied = _frontier_ancestors(recorded)
    if required_revision not in applied:
        return [f"migration prerequisite {required_revision} is not recorded"]
    return verify_frontier(connection, recorded)


def verify_frontier(connection: Connection, revisions) -> list[str]:
    """Verify a known, non-redundant DAG frontier without guessing from schema."""
    applied = _frontier_ancestors(revisions)
    failures = preflight(connection, nullable_google_sub=("0002_auth_identities" in applied), secure_catalogue=("0005_secure_catalogue" in applied))
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "0002_auth_identities" in applied:
        if "auth_identities" not in tables:
            failures.append("table 'auth_identities' is missing")
        else:
            failures += _verify_table_definition(inspector, "auth_identities", secure_catalogue=("0005_secure_catalogue" in applied))
        if not any(i["name"] == "ix_users_google_sub" and i.get("unique") for i in inspector.get_indexes("users")):
            failures.append("unique index 'ix_users_google_sub' is missing")
    if "0003_processing_jobs" in applied:
        for table_name in ("processing_jobs", "deletion_intents"):
            if table_name not in tables:
                failures.append(f"table '{table_name}' is missing")
                continue
            failures += _verify_table_definition(
                inspector,
                table_name,
                ocr_review=("0009_ocr_review" in applied and table_name == "processing_jobs"),
                native_text=("0014_native_text" in applied and table_name == "processing_jobs"),
                storage_quota=(
                    "0011_legacy_room_storage_quota" in applied
                    and table_name == "deletion_intents"
                ),
                extra_unique=(
                    PARENT_COMPOSITE_UNIQUES["processing_jobs"]
                    if "0004_retrieval" in applied and table_name == "processing_jobs"
                    else None
                ),
            )
    if "0004_retrieval" in applied:
        for table_name, expected_unique in PARENT_COMPOSITE_UNIQUES.items():
            if table_name not in tables:
                continue
            actual_unique = {
                item["name"]: tuple(item["column_names"])
                for item in inspector.get_unique_constraints(table_name)
            }
            for constraint_name, columns in expected_unique.items():
                if actual_unique.get(constraint_name) != columns:
                    failures.append(
                        f"unique constraint '{constraint_name}' on {table_name} differs"
                    )
        for table_name in ("record_chunks", "record_embeddings"):
            if table_name not in tables:
                failures.append(f"table '{table_name}' is missing")
                continue
            failures += _verify_table_definition(inspector, table_name)
    if "0005_secure_catalogue" in applied:
        failures += _verify_secure_schema(connection, storage_quota=(QUOTA_REVISION in applied),
                                          reconciliation=(RECONCILIATION_REVISION in applied))
    if QUOTA_REVISION in applied and connection.dialect.name == "sqlite":
        actual = {r[0]: _normalize_check(r[1]) for r in connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'quota_%'")}
        expected = {sql.split()[2]: _normalize_check(sql) for sql in _quota_migration().graph_triggers()}
        if actual != expected:
            failures.append("secure quota graph fences differ")
    if "0006_family_graph" in applied:
        for table_name in ("family_people", "family_relationships"):
            if table_name not in tables:
                failures.append(f"table '{table_name}' is missing")
                continue
            failures += _verify_table_definition(inspector, table_name)
    if "0008_upload_file_uploader" in applied and "upload_files" in tables:
        columns = {c["name"]: c for c in inspector.get_columns("upload_files")}
        uploader = columns.get("file_uploader_user_id")
        if uploader is None:
            failures.append("column 'upload_files.file_uploader_user_id' is missing")
        else:
            if _canonical_type_signature(uploader["type"]) != ("string", None):
                failures.append("column 'upload_files.file_uploader_user_id' has unexpected type")
            if not uploader["nullable"] or uploader.get("default") is not None:
                failures.append("column 'upload_files.file_uploader_user_id' has unexpected nullability or server default")
        fks = [fk for fk in inspector.get_foreign_keys("upload_files")
               if "file_uploader_user_id" in fk["constrained_columns"]]
        if len(fks) != 1 or (tuple(fks[0]["constrained_columns"]), fks[0]["referred_table"],
                            tuple(fks[0]["referred_columns"]),
                            (fks[0].get("options") or {}).get("ondelete", "").upper()) != (
                                ("file_uploader_user_id",), "users", ("id",), "SET NULL"):
            failures.append("upload_files uploader foreign key differs")
    if "0009_ocr_review" in applied and "processing_jobs" in tables:
        review = {c["name"]: c for c in inspector.get_columns("processing_jobs")}.get("review_json")
        if review is None:
            failures.append("column 'processing_jobs.review_json' is missing")
        elif (_canonical_type_signature(review["type"]) != ("text",)
              or not review["nullable"] or review.get("default") is not None):
            failures.append("column 'processing_jobs.review_json' differs")
    if "0011_legacy_room_storage_quota" in applied:
        if "legacy_storage_allocations" not in tables:
            failures.append("table 'legacy_storage_allocations' is missing")
        else:
            failures += _verify_table_definition(
                inspector, "legacy_storage_allocations"
            )
        if "upload_files" in tables:
            columns = {
                column["name"]: column
                for column in inspector.get_columns("upload_files")
            }
            allocation = columns.get("storage_allocation_id")
            if allocation is None:
                failures.append(
                    "column 'upload_files.storage_allocation_id' is missing"
                )
            elif (
                _canonical_type_signature(allocation["type"]) != ("string", None)
                or not allocation["nullable"]
                or allocation.get("default") is not None
            ):
                failures.append(
                    "column 'upload_files.storage_allocation_id' differs"
                )
            unique = {
                item["name"]: tuple(item["column_names"])
                for item in inspector.get_unique_constraints("upload_files")
            }
            if unique.get("uq_upload_file_storage_allocation") != (
                "storage_allocation_id",
            ):
                failures.append(
                    "unique constraint 'uq_upload_file_storage_allocation' differs"
                )
            links = [
                item
                for item in inspector.get_foreign_keys("upload_files")
                if item["constrained_columns"] == ["storage_allocation_id"]
            ]
            if len(links) != 1 or (
                links[0]["referred_table"],
                tuple(links[0]["referred_columns"]),
            ) != ("legacy_storage_allocations", ("id",)):
                failures.append("upload_files storage allocation foreign key differs")
    else:
        if "legacy_storage_allocations" in tables:
            failures.append("catalogue contains unexpected storage allocation table")
        for table_name in ("upload_files", "deletion_intents"):
            if table_name in tables and "storage_allocation_id" in {
                column["name"] for column in inspector.get_columns(table_name)
            }:
                failures.append(
                    f"catalogue contains unexpected {table_name}.storage_allocation_id"
                )
    album_tables = set(_home_migration().ALBUM_TABLES)
    if "0006_photo_albums" in applied:
        failures += _verify_album_schema(connection, extended=("0007_video_albums_reads" in applied), storage_quota=(QUOTA_REVISION in applied))
    elif album_tables & tables:
        failures.append("catalogue contains unexpected album tables")
    if "0006_family_graph" not in applied and {"family_people", "family_relationships"} & tables:
        failures.append("catalogue contains unexpected family graph tables")
    if "upload_files" in tables and "0008_upload_file_uploader" not in applied:
        if "file_uploader_user_id" in {c["name"] for c in inspector.get_columns("upload_files")}:
            failures.append("catalogue contains unexpected file_uploader_user_id")
    if "0016_family_context" in applied:
        failures += _verify_family_context_schema(connection)
    elif "context_entities" in tables or "external_dataset_refs" in tables:
        failures.append("catalogue contains unexpected M3 context schema")
    if "0015_record_privacy" in applied:
        failures += _verify_record_schema(connection)
    elif "records" in tables or "is_active" in {c["name"] for c in inspector.get_columns("family_members")}:
        failures.append("catalogue contains unexpected record policy schema")
    return failures


def _verify_record_schema(connection: Connection) -> list[str]:
    from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

    spec = importlib.util.spec_from_file_location("ambient_frozen_record_migration", ALEMBIC_SCRIPT_LOCATION / "versions" / "0015_record_privacy.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("record policy migration cannot be loaded")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    metadata = migration.schema_metadata()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    failures = []
    membership = next((c for c in inspector.get_columns("family_members") if c["name"] == "is_active"), None)
    if membership is None or membership["nullable"] or str(membership["type"]).upper() != "BOOLEAN" or _normalize_default(membership["default"]) not in ("1", "true"):
        failures.append("family_members.is_active definition differs")
    room = next((c for c in inspector.get_columns("family_rooms") if c["name"] == "is_active"), None)
    if room is None or room["nullable"] or str(room["type"]).upper() != "BOOLEAN" or _normalize_default(room["default"]) not in ("1", "true"):
        failures.append("family_rooms.is_active definition differs")
    for name in migration.TABLES:
        if name not in tables:
            failures.append(f"table '{name}' is missing")
            continue
        expected = metadata.tables[name]
        columns = {c["name"]: (str(c["type"]).upper(), bool(c["nullable"]), _normalize_default(c.get("default"))) for c in inspector.get_columns(name)}
        wanted = {c.name: (str(c.type.compile(dialect=connection.dialect)).upper(), c.nullable, _normalize_default(c.server_default.arg.compile(dialect=connection.dialect) if hasattr(c.server_default.arg, "compile") else c.server_default.arg) if c.server_default is not None else None) for c in expected.columns}
        if columns != wanted:
            failures.append(f"table '{name}' columns differ")
        if tuple(inspector.get_pk_constraint(name)["constrained_columns"]) != tuple(c.name for c in expected.primary_key):
            failures.append(f"table '{name}' primary key differs")
        uniques = {(c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints(name)}
        if uniques != {(c.name, tuple(col.name for col in c.columns)) for c in expected.constraints if isinstance(c, UniqueConstraint)}:
            failures.append(f"table '{name}' unique constraints differ")
        checks = {(c["name"], _normalize_check(c["sqltext"])) for c in inspector.get_check_constraints(name)}
        if checks != {(c.name, _normalize_check(c.sqltext)) for c in expected.constraints if isinstance(c, CheckConstraint)}:
            failures.append(f"table '{name}' CHECK constraints differ")
        foreign_keys = {(tuple(c["constrained_columns"]), c["referred_table"], tuple(c["referred_columns"]), (c.get("options") or {}).get("ondelete", "NO ACTION")) for c in inspector.get_foreign_keys(name)}
        if foreign_keys != {(tuple(c.column_keys), c.referred_table.name, tuple(e.column.name for e in c.elements), c.ondelete or "NO ACTION") for c in expected.constraints if isinstance(c, ForeignKeyConstraint)}:
            failures.append(f"table '{name}' foreign keys differ")
        indexes = {(i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(name) if not i.get("duplicates_constraint")}
        if indexes != {(i.name, tuple(c.name for c in i.columns), bool(i.unique)) for i in expected.indexes}:
            failures.append(f"table '{name}' indexes differ")
    if connection.dialect.name == "sqlite":
        triggers = {row[0]: _normalize_check(row[1]) for row in connection.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name LIKE 'record_audits_no_%' OR name LIKE 'm2_%')")}
        wanted_triggers = {sql.split()[2]: _normalize_check(sql) for sql in migration.trigger_statements()}
        if triggers != wanted_triggers:
            failures.append("record audit immutability triggers differ")
    return failures


def _secure_migration():
    path = ALEMBIC_SCRIPT_LOCATION / "versions" / "0005_secure_catalogue.py"
    spec = importlib.util.spec_from_file_location("ambient_frozen_secure_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _album_migration():
    path = ALEMBIC_SCRIPT_LOCATION / "versions" / "0006_photo_albums.py"
    spec = importlib.util.spec_from_file_location("ambient_frozen_album_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _home_migration():
    path = ALEMBIC_SCRIPT_LOCATION / "versions" / "0007_video_albums_reads.py"
    spec = importlib.util.spec_from_file_location("ambient_frozen_home_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_album_schema(connection, extended=False, storage_quota=False):
    """Verify frozen LEGACY album columns, constraints, indexes and mode fences."""
    from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

    failures = []
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    migration = _home_migration() if extended else _album_migration()
    if not extended and set(_home_migration().NEW_TABLES) & tables:
        failures.append("0006 catalogue contains unsupported home tables")
    metadata = migration.schema_metadata()
    for name in migration.ALBUM_TABLES:
        if name not in tables:
            failures.append(f"table '{name}' is missing")
            continue
        expected = metadata.tables[name]
        actual_columns = {
            c["name"]: (str(c["type"]).upper(), bool(c["nullable"]), _normalize_default(c.get("default")))
            for c in inspector.get_columns(name)
        }
        expected_columns = {
            c.name: (str(c.type.compile(dialect=connection.dialect)).upper(), c.nullable,
                     _normalize_default(c.server_default.arg) if c.server_default is not None else None)
            for c in expected.columns
        }
        if actual_columns != expected_columns:
            failures.append(f"table '{name}' columns differ")
        if tuple(inspector.get_pk_constraint(name)["constrained_columns"]) != tuple(c.name for c in expected.primary_key):
            failures.append(f"table '{name}' primary key differs")
        actual_unique = {(c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints(name)}
        expected_unique = {(c.name, tuple(col.name for col in c.columns)) for c in expected.constraints if isinstance(c, UniqueConstraint)}
        if actual_unique != expected_unique:
            failures.append(f"table '{name}' unique constraints differ")
        actual_checks = {(c["name"], _normalize_check(c["sqltext"])) for c in inspector.get_check_constraints(name)}
        expected_checks = {(c.name, _normalize_check(c.sqltext)) for c in expected.constraints if isinstance(c, CheckConstraint)}
        if actual_checks != expected_checks:
            failures.append(f"table '{name}' CHECK constraints differ")
        actual_fks = {(tuple(c["constrained_columns"]), c["referred_table"], tuple(c["referred_columns"]),
                       (c.get("options") or {}).get("ondelete", "NO ACTION"),
                       (c.get("options") or {}).get("onupdate", "NO ACTION")) for c in inspector.get_foreign_keys(name)}
        expected_fks = {(tuple(c.column_keys), c.referred_table.name, tuple(e.column.name for e in c.elements),
                         c.ondelete or "NO ACTION", c.onupdate or "NO ACTION")
                        for c in expected.constraints if isinstance(c, ForeignKeyConstraint)}
        if actual_fks != expected_fks:
            failures.append(f"table '{name}' foreign keys differ")
        actual_indexes = {(i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(name)
                          if not i.get("duplicates_constraint")}
        expected_indexes = {(i.name, tuple(c.name for c in i.columns), bool(i.unique)) for i in expected.indexes}
        if actual_indexes != expected_indexes:
            failures.append(f"table '{name}' indexes differ")
    if connection.dialect.name == "sqlite":
        actual_triggers = {row[0]: _normalize_check(row[1]) for row in connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'album_%'")}
        expected_triggers = {sql.split()[2]: _normalize_check(sql) for sql in (
            _quota_migration().album_triggers() if storage_quota else migration.trigger_statements())}
        if actual_triggers != expected_triggers:
            failures.append("album catalogue triggers differ")
    if "security_control" in tables:
        rows = connection.exec_driver_sql("SELECT mode,admission,runtime_generation FROM security_control").all()
        if storage_quota or connection.info.get("secure_quota_upgrade"):
            if len(rows) != 1 or rows[0][0] not in ("LEGACY", "CUTOVER", "SECURE", "QUARANTINED"):
                failures.append("album catalogue has invalid security control")
            elif rows[0][0] != "LEGACY" and any(
                    connection.exec_driver_sql(f"SELECT 1 FROM {t} LIMIT 1").first()
                    for t in _quota_migration().PLAINTEXT_TABLES):
                failures.append("non-LEGACY plaintext extensions must remain empty")
        elif rows != [("LEGACY", "CLOSED", None)]:
            failures.append("album catalogue must remain closed LEGACY")
    return failures


def _verify_secure_schema(connection, storage_quota=False, reconciliation=False):
    """Check columns, NULL/PK/FK/unique/CHECK/partial-index semantics and every fence."""
    from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
    from app.core.database import Base
    from app import models  # noqa: F401

    failures = []
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    migration = _secure_migration()
    reconciliation_migration = _reconciliation_migration()
    extra_tables = reconciliation_migration.NEW_TABLES if reconciliation else ()
    extra_metadata = reconciliation_migration.schema_metadata()
    if not reconciliation and set(reconciliation_migration.NEW_TABLES) & tables:
        failures.append("unexpected secure reconciliation tables")
    for name in (*migration.SECURE_TABLES, *extra_tables):
        if name not in tables:
            failures.append(f"table '{name}' is missing")
            continue
        expected = extra_metadata.tables[name] if name in extra_tables else Base.metadata.tables[name]
        columns = {
            c["name"]: (str(c["type"]).upper(), bool(c["nullable"]), _normalize_default(c.get("default")))
            for c in inspector.get_columns(name)
        }
        want_columns = {
            c.name: (str(c.type.compile(dialect=connection.dialect)).upper(), c.nullable,
                     _normalize_default(c.server_default.arg) if c.server_default else None)
            for c in expected.columns
            if storage_quota or c.name != "reserved_ciphertext_bytes"
        }
        if columns != want_columns:
            failures.append(f"table '{name}' columns differ")
        if tuple(inspector.get_pk_constraint(name)["constrained_columns"]) != tuple(c.name for c in expected.primary_key):
            failures.append(f"table '{name}' primary key differs")
        actual_unique = {(c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints(name)}
        want_unique = {(c.name, tuple(col.name for col in c.columns)) for c in expected.constraints if isinstance(c, UniqueConstraint)}
        if actual_unique != want_unique:
            failures.append(f"table '{name}' unique constraints differ")
        actual_checks = {(c["name"], _normalize_check(c["sqltext"])) for c in inspector.get_check_constraints(name)}
        want_checks = {(c.name, _normalize_check(c.sqltext)) for c in expected.constraints if isinstance(c, CheckConstraint)
                       and (storage_quota or c.name != "ck_secure_operation_reserved_bytes")}
        if actual_checks != want_checks:
            failures.append(f"table '{name}' CHECK constraints differ")
        actual_fk = {(tuple(c["constrained_columns"]), c["referred_table"], tuple(c["referred_columns"]),
                      (c.get("options") or {}).get("ondelete", "NO ACTION")) for c in inspector.get_foreign_keys(name)}
        want_fk = {(tuple(c.column_keys), c.referred_table.name, tuple(e.column.name for e in c.elements),
                    c.ondelete or "NO ACTION") for c in expected.constraints if isinstance(c, ForeignKeyConstraint)}
        if actual_fk != want_fk:
            failures.append(f"table '{name}' foreign keys differ")
        actual_indexes = {(i["name"], tuple(i["column_names"]), bool(i["unique"]),
                           _normalize_check((i.get("dialect_options") or {}).get("sqlite_where", ""))) for i in inspector.get_indexes(name)}
        want_indexes = {(i.name, tuple(c.name for c in i.columns), bool(i.unique),
                         _normalize_check(i.dialect_options["sqlite"].get("where", ""))) for i in expected.indexes}
        if actual_indexes != want_indexes:
            failures.append(f"table '{name}' indexes differ")
    for name, columns in migration.NULLABLE_COLUMNS.items():
        if name in tables:
            observed = {c["name"]: c["nullable"] for c in inspector.get_columns(name)}
            if any(observed.get(column) is not True for column in columns):
                failures.append(f"table '{name}' secure nullability differs")
    if connection.dialect.name == "sqlite":
        actual_triggers = {row[0]: _normalize_check(row[1]) for row in connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'm4_%'")}
        expected_triggers = {sql.split()[2]: _normalize_check(sql) for sql in (
            _quota_migration().operation_triggers() if storage_quota else migration.trigger_statements())}
        if actual_triggers != expected_triggers:
            failures.append("secure catalogue triggers differ")
    if connection.dialect.name == "sqlite":
        actual = {row[0]: _normalize_check(row[1]) for row in connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('secure_create_bindings','secure_reconciliation_events')")}
        expected = {sql.split()[2]: _normalize_check(sql)
                    for sql in reconciliation_migration.trigger_statements()} if reconciliation else {}
        if actual != expected:
            failures.append("secure reconciliation triggers differ")
    if "security_control" in tables and connection.exec_driver_sql("SELECT count(*) FROM security_control WHERE id=1").scalar_one() != 1:
        failures.append("secure catalogue control row is missing")
    return failures


def _related_user_fk_present(inspector) -> bool:
    related_fks = [
        fk for fk in inspector.get_foreign_keys("family_members")
        if fk["constrained_columns"] == ["related_to_user_id"]
    ]
    if any(
        fk["referred_table"] != "users" or fk["referred_columns"] != ["id"]
        for fk in related_fks
    ):
        raise RuntimeError(
            "family_members.related_to_user_id has a conflicting foreign key; expected users.id"
        )
    return bool(related_fks)


def count_orphaned_related_users(connection: Connection) -> int:
    """Count only (A3): rows whose related_to_user_id names no user. Never mutates."""
    members = table("family_members", column("related_to_user_id", String()))
    users = table("users", column("id", String()))
    return connection.execute(
        select(func.count()).select_from(members).where(
            members.c.related_to_user_id.is_not(None),
            members.c.related_to_user_id.not_in(select(users.c.id)),
        )
    ).scalar_one()


def normalize_related_user_fk(connection: Connection) -> None:
    """Add the canonical enforced FK family_members.related_to_user_id -> users.id
    (batch mode rebuilds the table on SQLite, plain ALTER TABLE elsewhere)."""
    operations = Operations(MigrationContext.configure(connection))
    with operations.batch_alter_table("family_members") as batch_op:
        batch_op.create_foreign_key(
            "fk_family_members_related_to_user_id_users",
            "users",
            ["related_to_user_id"],
            ["id"],
        )


def _build_alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def adopt(database_url: str, *, check_only: bool = False, target_revision: str | None = None) -> str:
    """A2 state table. Returns the detected state; raises RuntimeError with no
    mutation for every ambiguous, unrecognized, or drifted state."""
    if target_revision is not None and target_revision not in KNOWN_REVISIONS:
        raise RuntimeError("Unrecognized adoption target revision")
    engine = migration_engine(database_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            recorded = recorded_revisions(connection)
            if not tables:
                state = "fresh"
            elif recorded is None:
                failures = preflight(connection)
                if failures:
                    raise RuntimeError("Alembic adoption preflight failed:\n" + "\n".join(f"- {f}" for f in failures))
                state = "legacy"
            else:
                failures = verify_frontier(connection, recorded)
                if failures:
                    raise RuntimeError(
                        f"database stamped {recorded} does not match that revision:\n"
                        + "\n".join(f"- {f}" for f in failures)
                    )
                state = ",".join(recorded)

            # Albums are LEGACY-only. Preserve existing non-LEGACY feature
            # catalogues too, rather than silently advancing them into albums.
            applied = _frontier_ancestors(recorded) if recorded else set()
            preserve_secure = False
            if "0005_secure_catalogue" in applied:
                mode = connection.exec_driver_sql("SELECT mode FROM security_control WHERE id=1").scalar_one()
                preserve_secure = mode != "LEGACY"
            target = target_revision or (None if preserve_secure else MERGED_HEAD)
            if target is not None:
                target_ancestors = _ancestors(target)
                if preserve_secure and "0006_photo_albums" in target_ancestors and target not in (QUOTA_REVISION, RECONCILIATION_REVISION, "0014_native_text", MERGED_HEAD):
                    raise RuntimeError("Album upgrade refused for non-LEGACY catalogue")
                if not applied <= target_ancestors:
                    raise RuntimeError("Adoption cannot downgrade or discard a database branch")

            needs_fk = state != "fresh" and not _related_user_fk_present(inspector)
            if needs_fk:
                orphans = count_orphaned_related_users(connection)
                if orphans:
                    raise RuntimeError(
                        f"family_members.related_to_user_id has {orphans} orphaned row(s); "
                        "refusing to adopt until a human resolves them (A3)"
                    )

        if check_only or target is None:
            return state

        if needs_fk:
            with engine.connect() as connection, migration_transaction(connection):
                normalize_related_user_fk(connection)

        config = _build_alembic_config(database_url)
        config.attributes["secure_quota_upgrade"] = (preserve_secure and target_revision in (QUOTA_REVISION, RECONCILIATION_REVISION, "0014_native_text", MERGED_HEAD)
                                                     and recorded == ["0009_ocr_review"])
        if state == "legacy":
            command.stamp(config, "0001_baseline")
        command.upgrade(config, target)
        return state
    finally:
        engine.dispose()



def _reconciliation_migration():
    path = ALEMBIC_SCRIPT_LOCATION / "versions" / "0013_secure_reconciliation.py"
    spec = importlib.util.spec_from_file_location("ambient_frozen_reconciliation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quota_migration():
    path = ALEMBIC_SCRIPT_LOCATION / "versions" / "0012_secure_room_storage_quota.py"
    spec = importlib.util.spec_from_file_location("ambient_frozen_quota_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_secure_quota_drain(connection):
    """No inference that an expired operation or a CLOSED runtime is dead."""
    query = "SELECT mode,admission,runtime_generation FROM security_control"
    if connection.dialect.name == "postgresql":
        query += " FOR UPDATE"
    rows = connection.exec_driver_sql(query).all()
    if len(rows) != 1 or rows[0][0] != "SECURE" or rows[0][1] != "CLOSED":
        raise RuntimeError("secure quota upgrade requires closed SECURE")
    if connection.exec_driver_sql("SELECT 1 FROM credential_generations WHERE state NOT IN ('DRAINED','FAILED') LIMIT 1").first():
        raise RuntimeError("secure quota upgrade requires drained generations")
    if connection.exec_driver_sql("SELECT 1 FROM secure_operations WHERE state NOT IN ('COMMITTED','ABANDONED','EXPIRED','FAILED') LIMIT 1").first():
        raise RuntimeError("secure quota upgrade requires terminal operations")
    if connection.exec_driver_sql("SELECT 1 FROM secure_operations o WHERE o.kind='PAYLOAD' AND o.domain='room' "
                                 "AND NOT EXISTS (SELECT 1 FROM encrypted_objects e WHERE e.object_id=o.candidate_object_id) LIMIT 1").first():
        raise RuntimeError("secure quota upgrade refuses unaccounted historical creates")
    tables = set(inspect(connection).get_table_names())
    if any(connection.exec_driver_sql(f"SELECT 1 FROM {t} LIMIT 1").first()
           for t in _quota_migration().PLAINTEXT_TABLES if t in tables):
        raise RuntimeError("secure quota upgrade requires empty plaintext extensions")


def bridge_secure_quota_upgrade(connection, target):
    """Explicit 0009 -> cumulative bridge, within the migration DDL transaction.

    Do not execute LEGACY-only old upgrade functions against SECURE or spoof mode.
    Install their exact empty frozen schema instead; verify it before advancing.
    """
    if target not in (QUOTA_REVISION, RECONCILIATION_REVISION, "0014_native_text", MERGED_HEAD) or recorded_revisions(connection) != ["0009_ocr_review"]:
        raise RuntimeError("secure quota bridge requires exact 0009 and a supported cumulative target")
    failures = verify_revision(connection, "0009_ocr_review")
    if failures:
        raise RuntimeError("secure quota bridge schema drift: " + "; ".join(failures))
    require_secure_quota_drain(connection)
    migration = _home_migration()
    metadata = migration.schema_metadata()
    for name in migration.ALBUM_TABLES:
        metadata.tables[name].create(connection)
    if connection.dialect.name == "sqlite":
        for sql in migration.trigger_statements():
            connection.exec_driver_sql(sql)
    connection.info["secure_quota_upgrade"] = True
    failures = verify_frontier(connection, (ALBUM_MERGE_REVISION,))
    if failures:
        raise RuntimeError("secure quota bridge schema mismatch: " + "; ".join(failures))
    connection.exec_driver_sql("UPDATE alembic_version SET version_num='0010_merge_album_family'")


def _verify_family_context_schema(connection: Connection) -> list[str]:
    spec = importlib.util.spec_from_file_location("ambient_frozen_context_migration", ALEMBIC_SCRIPT_LOCATION / "versions" / "0016_family_context.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("M3 schema snapshot unavailable")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration.verify(connection)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Adopt Alembic for a database (A2 state table). Never prints the URL.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), help="defaults to $DATABASE_URL")
    parser.add_argument("--target-revision", choices=REVISION_CHAIN, help="explicit schema target; non-LEGACY defaults to its current revision")
    parser.add_argument("--check", action="store_true", help="report the detected state and mutate nothing")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        state = adopt(args.database_url, check_only=args.check, target_revision=args.target_revision)
    except Exception as error:
        parser.exit(1, f"error: {type(error).__name__}\n")
    print(state)
