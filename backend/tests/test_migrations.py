"""Alembic adoption tests (FOUNDATION_IDENTITY_MIGRATION_DECISION.md D3).

Builds temporary SQLite databases per test. The "legacy" fixture is assembled
from the exact pre-Alembic SQLAlchemy metadata (the shape ``create_all``
produced before the three retired ``ensure_*`` startup helpers ran) plus those
same helpers' raw ALTER statements reproduced here — never from the new
Alembic migration itself — so adoption is proven against the real historical
path, not a tautology.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from types import ModuleType
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    inspect,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from conftest import fk_enforced_engine, postgres_url_or_skip
from scripts.adopt_alembic import (
    BACKEND_ROOT,
    _build_alembic_config,
    adopt,
    count_orphaned_related_users,
    migration_engine,
    migration_transaction,
    preflight,
    recorded_revisions,
    verify_revision,
)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _head_revision() -> str:
    config = _build_alembic_config("sqlite://")
    return ScriptDirectory.from_config(config).get_current_head()


def _legacy_metadata(*, users_email_nullable: bool = False) -> MetaData:
    """Pre-Alembic ``create_all`` shape, before the three ensure_* helpers ran."""
    metadata = MetaData()

    Table(
        "users",
        metadata,
        Column("id", String(), primary_key=True),
        Column("google_sub", String(), nullable=False, unique=True, index=True),
        Column("email", String(), nullable=users_email_nullable, unique=True, index=True),
        Column("name", String(), nullable=False),
        Column("profile_image", String(), nullable=True),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    Table(
        "family_rooms",
        metadata,
        Column("id", String(), primary_key=True),
        Column("name", String(), nullable=False),
        Column("invite_code", String(), nullable=False, unique=True, index=True),
        Column("owner_user_id", String(), ForeignKey("users.id"), nullable=False),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    Table(
        "family_members",
        metadata,
        Column("id", String(), primary_key=True),
        Column("room_id", String(), ForeignKey("family_rooms.id"), nullable=False),
        Column("user_id", String(), ForeignKey("users.id"), nullable=False),
        Column("role", String(), nullable=False, server_default="member"),
        Column("joined_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        UniqueConstraint("room_id", "user_id", name="uq_room_user"),
    )
    Table(
        "uploads",
        metadata,
        Column("id", String(), primary_key=True),
        Column("room_id", String(), ForeignKey("family_rooms.id"), nullable=False),
        Column("uploader_user_id", String(), ForeignKey("users.id"), nullable=False),
        Column("type", String(), nullable=False),
        Column("title", String(), nullable=False),
        Column("description", Text(), nullable=True),
        Column("status", String(), nullable=False, server_default="uploaded"),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    Table(
        "upload_files",
        metadata,
        Column("id", String(), primary_key=True),
        Column("upload_id", String(), ForeignKey("uploads.id"), nullable=False),
        Column("storage_bucket", String(), nullable=False),
        Column("storage_path", String(), nullable=False),
        Column("mime_type", String(), nullable=False),
        Column("file_size", Integer(), nullable=True),
        Column("encrypted", Boolean(), nullable=False, server_default=text("1")),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    return metadata


def _apply_ensure_user_profile_columns(engine) -> None:
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing = {c["name"] for c in inspector.get_columns("users")}
        statements = {
            "age": "ALTER TABLE users ADD COLUMN age INTEGER",
            "gender": "ALTER TABLE users ADD COLUMN gender VARCHAR",
            "phone": "ALTER TABLE users ADD COLUMN phone VARCHAR",
            "profile_chunk": "ALTER TABLE users ADD COLUMN profile_chunk TEXT",
        }
        for name, statement in statements.items():
            if name not in existing:
                connection.execute(text(statement))


def _apply_ensure_user_auth_columns(engine, *, create_index: bool = True) -> None:
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing = {c["name"] for c in inspector.get_columns("users")}
        statements = {
            "username": "ALTER TABLE users ADD COLUMN username VARCHAR",
            "password_hash": "ALTER TABLE users ADD COLUMN password_hash VARCHAR",
        }
        for name, statement in statements.items():
            if name not in existing:
                connection.execute(text(statement))
        if create_index:
            connection.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)")
            )


def _apply_ensure_family_member_relation_columns(engine) -> None:
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing = {c["name"] for c in inspector.get_columns("family_members")}
        statements = {
            "related_to_user_id": "ALTER TABLE family_members ADD COLUMN related_to_user_id VARCHAR",
            "relation_to_related_user": "ALTER TABLE family_members ADD COLUMN relation_to_related_user VARCHAR",
        }
        for name, statement in statements.items():
            if name not in existing:
                connection.execute(text(statement))


def build_legacy_database(
    url: str,
    *,
    apply_auth_columns: bool = True,
    apply_profile_columns: bool = True,
    apply_relation_columns: bool = True,
    create_username_index: bool = True,
    users_email_nullable: bool = False,
):
    """Reproduce the exact runtime path: create_all() (old shape) + ensure_* helpers."""
    engine = fk_enforced_engine(url)
    _legacy_metadata(users_email_nullable=users_email_nullable).create_all(engine)
    if apply_profile_columns:
        _apply_ensure_user_profile_columns(engine)
    if apply_auth_columns:
        _apply_ensure_user_auth_columns(engine, create_index=create_username_index)
    if apply_relation_columns:
        _apply_ensure_family_member_relation_columns(engine)
    return engine


def _normalize_default(value) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).replace("(", "").replace(")", "").split()).lower()


def _fingerprint_type(dialect_name: str, table_name: str, column_name: str, value) -> str | tuple:
    if (table_name, column_name) == ("record_embeddings", "embedding"):
        if dialect_name == "postgresql":
            return ("vector", getattr(value, "dim", None))
    return str(value).upper()


def schema_fingerprint(engine) -> dict:
    """Complete schema semantics (A3): columns incl. type/nullability/server default,
    indexes incl. uniqueness, unique constraints, FKs, CHECKs. Constraint names are
    compared for indexes/uniques/CHECKs but not for FKs (SQLite reflects unnamed
    FKs as None, PostgreSQL auto-names them)."""
    inspector = inspect(engine)
    dialect_name = engine.dialect.name
    tables: dict = {}
    for table_name in sorted(t for t in inspector.get_table_names() if t != "alembic_version"):
        tables[table_name] = {
            "columns": sorted(
                (
                    c["name"],
                    _fingerprint_type(dialect_name, table_name, c["name"], c["type"]),
                    bool(c["nullable"]),
                    _normalize_default(c.get("default")),
                )
                for c in inspector.get_columns(table_name)
            ),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i.get("unique"))) for i in inspector.get_indexes(table_name)
            ),
            "unique": sorted((u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table_name)),
            "foreign_keys": sorted(
                (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                for f in inspector.get_foreign_keys(table_name)
            ),
            "checks": sorted(
                (c["name"], " ".join(str(c["sqltext"]).split()).lower()) for c in inspector.get_check_constraints(table_name)
            ),
        }
    return {"dialect": dialect_name, "tables": tables}


def _version_rows(url: str) -> list[tuple]:
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).all()
    finally:
        engine.dispose()


def _fk_check_clean(url: str) -> bool:
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            return connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def recorded_revisions_of(url: str):
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            return recorded_revisions(connection)
    finally:
        engine.dispose()


def _seed_legacy_rows(engine) -> None:
    """Rows in every legacy table, including a valid related_to_user_id, so the batch
    rebuilds in 0002 and the FK normalization run against real child rows."""
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, google_sub, username, password_hash, email, name) VALUES "
            "('u-local', 'local:parent', 'parent', 'pbkdf2_sha256$390000$salt$hash', 'parent@example.test', 'Parent'), "
            "('u-google', 'google-sub-child', NULL, NULL, 'child@example.test', 'Child')"
        ))
        connection.execute(text("INSERT INTO family_rooms (id, name, invite_code, owner_user_id) VALUES ('r-1', 'Home', 'INV00001', 'u-local')"))
        connection.execute(text(
            "INSERT INTO family_members (id, room_id, user_id, role, related_to_user_id, relation_to_related_user) VALUES "
            "('m-1', 'r-1', 'u-local', 'owner', NULL, NULL), ('m-2', 'r-1', 'u-google', 'member', 'u-local', 'child')"
        ))
        connection.execute(text("INSERT INTO uploads (id, room_id, uploader_user_id, type, title) VALUES ('rec-1', 'r-1', 'u-local', 'image', 'Receipt')"))
        connection.execute(text(
            "INSERT INTO upload_files "
            "(id, upload_id, storage_bucket, storage_path, mime_type, file_size, encrypted) VALUES "
            "('f-1', 'rec-1', 'local-media', 'rec-1.png', 'image/png', 4, 0)"
        ))
    root = Path(os.environ["AMBIENT_LEGACY_STORAGE_ROOT"])
    (root / "rec-1.png").write_bytes(b"data")


def _identity_set(url: str) -> set[tuple]:
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            return set(connection.execute(text(
                "SELECT user_id, provider, provider_subject, credential_hash, provider_email FROM auth_identities"
            )).all())
    finally:
        engine.dispose()


def _signup(url: str, username: str = "alice_test", email: str = "alice@acme-corp.com") -> None:
    from app.core import database
    from app.main import app

    engine = fk_enforced_engine(url)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[database.get_db] = override_get_db
    try:
        response = TestClient(app).post(
            "/api/v1/auth/signup",
            json={"username": username, "email": email, "password": "correct horse battery staple 9", "name": "Alice"},
        )
        assert response.status_code in (200, 201), response.json()
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_fresh_and_adopted_legacy_fingerprints_match(tmp_path):
    fresh_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    assert adopt(fresh_url) == "fresh"
    legacy_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    build_legacy_database(legacy_url).dispose()
    assert adopt(legacy_url) == "legacy"

    fresh_engine, legacy_engine = fk_enforced_engine(fresh_url), fk_enforced_engine(legacy_url)
    try:
        fresh, legacy = schema_fingerprint(fresh_engine), schema_fingerprint(legacy_engine)
        assert fresh["dialect"] == legacy["dialect"] == "sqlite"
        assert (
            "embedding", "TEXT", False, None
        ) in fresh["tables"]["record_embeddings"]["columns"]
        assert fresh == legacy
        assert (("related_to_user_id",), "users", ("id",)) in fresh["tables"]["family_members"]["foreign_keys"]
    finally:
        fresh_engine.dispose()
        legacy_engine.dispose()
    assert _fk_check_clean(legacy_url)


def test_adoption_is_idempotent(tmp_path):
    db_path = tmp_path / "idempotent.db"
    url = f"sqlite:///{db_path}"
    adopt(url)
    before = sha256_of(db_path)
    assert adopt(url) == _head_revision()
    assert sha256_of(db_path) == before
    assert _version_rows(url) == [(_head_revision(),)]


def test_adopt_after_signup_is_noop(tmp_path):
    """AUDIT-1 #6: a post-cutover signup (google_sub NULL) must not make re-adoption fail."""
    db_path = tmp_path / "signup.db"
    url = f"sqlite:///{db_path}"
    adopt(url)
    _signup(url)

    before = sha256_of(db_path)
    assert adopt(url) == _head_revision()
    assert sha256_of(db_path) == before


def test_adoption_with_rows_under_enforced_fk(tmp_path):
    url = f"sqlite:///{tmp_path / 'rows.db'}"
    engine = build_legacy_database(url)
    _seed_legacy_rows(engine)
    engine.dispose()

    assert adopt(url) == "legacy"

    assert _fk_check_clean(url)
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 2
            assert connection.execute(text("SELECT COUNT(*) FROM family_members")).scalar_one() == 2
            assert connection.execute(text("SELECT COUNT(*) FROM upload_files")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM auth_identities")).scalar_one() == 2
            with pytest.raises(IntegrityError):
                connection.execute(text(
                    "INSERT INTO family_members (id, room_id, user_id, role, related_to_user_id) "
                    "VALUES ('m-x', 'r-1', 'u-google', 'member', 'nobody')"
                ))
    finally:
        engine.dispose()


def test_orphaned_related_user_blocks_adoption(tmp_path):
    db_path = tmp_path / "orphan.db"
    url = f"sqlite:///{db_path}"
    engine = build_legacy_database(url)
    _seed_legacy_rows(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE family_members SET related_to_user_id = 'u-gone' WHERE id = 'm-2'"))
    with engine.connect() as connection:
        assert count_orphaned_related_users(connection) == 1
    engine.dispose()

    before = sha256_of(db_path)
    with pytest.raises(RuntimeError) as exc_info:
        adopt(url)
    assert "1 orphaned" in str(exc_info.value)
    assert "u-gone" not in str(exc_info.value)
    assert sha256_of(db_path) == before
    assert recorded_revisions_of(url) is None


def test_conflicting_related_user_fk_blocks_adoption_without_mutation(tmp_path):
    db_path = tmp_path / "wrong-related-fk.db"
    url = f"sqlite:///{db_path}"
    build_legacy_database(url).dispose()
    engine = migration_engine(url)
    try:
        with engine.connect() as connection, migration_transaction(connection):
            operations = Operations(MigrationContext.configure(connection))
            with operations.batch_alter_table("family_members") as batch_op:
                batch_op.create_foreign_key(
                    "fk_family_members_related_to_user_id_users_email",
                    "users",
                    ["related_to_user_id"],
                    ["email"],
                )
    finally:
        engine.dispose()

    before = sha256_of(db_path)
    with pytest.raises(RuntimeError, match="users.id"):
        adopt(url)
    assert sha256_of(db_path) == before
    assert recorded_revisions_of(url) is None


def test_stamped_0001_without_related_fk_is_normalized_and_advanced(tmp_path):
    url = f"sqlite:///{tmp_path / 'stamped.db'}"
    engine = build_legacy_database(url)
    _seed_legacy_rows(engine)
    engine.dispose()
    command.stamp(_build_alembic_config(url), "0001_baseline")

    assert adopt(url) == "0001_baseline"
    assert _version_rows(url) == [(_head_revision(),)]
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            assert (("related_to_user_id",), "users", ("id",)) in schema_fingerprint(engine)["tables"]["family_members"]["foreign_keys"]
    finally:
        engine.dispose()
    assert _fk_check_clean(url)


@pytest.mark.parametrize(
    "version_rows, expected_fragment",
    [
        ([], "nonempty and unique"),
        (["0001_baseline", "0002_auth_identities"], "redundant ancestors"),
        (["9999_bogus"], "Unrecognized revision '9999_bogus'"),
        (["0002_auth_identities"], "does not match"),
    ],
)
def test_adopt_fails_closed_on_ambiguous_or_drifted_version_state(tmp_path, version_rows, expected_fragment):
    db_path = tmp_path / "state.db"
    url = f"sqlite:///{db_path}"
    engine = build_legacy_database(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        for revision in version_rows:
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": revision})
    engine.dispose()

    before = sha256_of(db_path)
    with pytest.raises(RuntimeError) as exc_info:
        adopt(url)
    assert expected_fragment in str(exc_info.value)
    assert sha256_of(db_path) == before


def test_adopt_rejects_named_but_incorrect_revision_constraints_without_mutation(tmp_path):
    db_path = tmp_path / "false-constraints.db"
    url = f"sqlite:///{db_path}"
    engine = build_legacy_database(url)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE auth_identities (
                stub_a VARCHAR,
                stub_b VARCHAR,
                CONSTRAINT uq_auth_identity_provider_subject UNIQUE (stub_a),
                CONSTRAINT uq_auth_identity_user_provider UNIQUE (stub_b),
                CONSTRAINT ck_auth_identity_local_requires_hash CHECK (1),
                CONSTRAINT ck_auth_identity_provider_allowed CHECK (1)
            )
        """))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0002_auth_identities')"))
    engine.dispose()

    before = sha256_of(db_path)
    with pytest.raises(RuntimeError, match="does not match"):
        adopt(url)
    assert sha256_of(db_path) == before


@pytest.mark.parametrize("provider_subject_type", ["TEXT", "VARCHAR(1)", "CHAR"])
def test_adopt_rejects_noncanonical_identity_string_types_without_mutation(
    tmp_path,
    provider_subject_type,
):
    db_path = tmp_path / "wrong-identity-type.db"
    url = f"sqlite:///{db_path}"
    engine = build_legacy_database(url)
    with engine.begin() as connection:
        connection.execute(text(f"""
            CREATE TABLE auth_identities (
                id VARCHAR NOT NULL PRIMARY KEY,
                user_id VARCHAR NOT NULL REFERENCES users (id),
                provider VARCHAR NOT NULL,
                provider_subject {provider_subject_type} NOT NULL,
                credential_hash VARCHAR,
                provider_email VARCHAR,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT uq_auth_identity_provider_subject UNIQUE (provider, provider_subject),
                CONSTRAINT uq_auth_identity_user_provider UNIQUE (user_id, provider),
                CONSTRAINT ck_auth_identity_local_requires_hash CHECK ((provider = 'local') = (credential_hash IS NOT NULL)),
                CONSTRAINT ck_auth_identity_provider_allowed CHECK (provider IN ('local', 'google'))
            )
        """))
        connection.execute(text("CREATE INDEX ix_auth_identities_user_id ON auth_identities (user_id)"))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0002_auth_identities')"))
    engine.dispose()

    before = sha256_of(db_path)
    with pytest.raises(RuntimeError, match="unexpected type"):
        adopt(url)
    assert sha256_of(db_path) == before


def test_verify_revision_reports_missing_0003_tables(tmp_path):
    url = f"sqlite:///{tmp_path / 'verify.db'}"
    adopt(url)
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            assert verify_revision(connection, _head_revision()) == []
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE deletion_intents")
        with engine.connect() as connection:
            assert any("deletion_intents" in f for f in verify_revision(connection, "0003_processing_jobs"))
    finally:
        engine.dispose()


@pytest.mark.parametrize("damage", ["missing_people", "missing_relationships", "relationship_constraints"])
def test_0006_graph_verifier_detects_missing_tables_and_constraint_drift(tmp_path, damage):
    url = f"sqlite:///{tmp_path / (damage + '.db')}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0006_family_graph")
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            assert verify_revision(connection, "0006_family_graph") == []
        with engine.begin() as connection:
            if damage == "missing_people":
                connection.exec_driver_sql("DROP TABLE family_people")
            elif damage == "missing_relationships":
                connection.exec_driver_sql("DROP TABLE family_relationships")
            else:
                connection.exec_driver_sql("DROP TABLE family_relationships")
                connection.exec_driver_sql(
                    "CREATE TABLE family_relationships ("
                    "id VARCHAR NOT NULL PRIMARY KEY, room_id VARCHAR NOT NULL, "
                    "from_person_id VARCHAR NOT NULL, to_person_id VARCHAR NOT NULL, "
                    "relation_type VARCHAR NOT NULL, created_at DATETIME NOT NULL, "
                    "updated_at DATETIME NOT NULL)"
                )
        with engine.connect() as connection:
            failures = verify_revision(connection, "0006_family_graph")
        expected_table = "family_people" if damage == "missing_people" else "family_relationships"
        assert any(expected_table in failure for failure in failures)
        if damage == "relationship_constraints":
            assert any("family_relationships" in failure and "unique constraints" in failure for failure in failures)
            assert any("family_relationships" in failure and "foreign keys" in failure for failure in failures)
            assert any("family_relationships" in failure and "CHECK constraints" in failure for failure in failures)
    finally:
        engine.dispose()


def test_verifier_ignores_unique_backing_indexes(tmp_path):
    from scripts import adopt_alembic

    url = f"sqlite:///{tmp_path / 'duplicate-index.db'}"
    command.upgrade(_build_alembic_config(url), "0004_retrieval")
    engine = fk_enforced_engine(url)
    try:
        inspector = inspect(engine)

        class InspectorWithUniqueBackingIndex:
            bind = inspector.bind

            def get_indexes(self, table_name):
                indexes = inspector.get_indexes(table_name)
                if table_name == "auth_identities":
                    return indexes + [{
                        "name": "uq_auth_identity_provider_subject",
                        "column_names": ["provider", "provider_subject"],
                        "unique": True,
                        "duplicates_constraint": "uq_auth_identity_provider_subject",
                    }]
                return indexes

            def __getattr__(self, name):
                return getattr(inspector, name)

        assert adopt_alembic._verify_table_definition(
            InspectorWithUniqueBackingIndex(), "auth_identities"
        ) == []
    finally:
        engine.dispose()


def test_0004_migration_freezes_vector_contract_from_live_orm(monkeypatch):
    fake_retrieval = ModuleType("app.models.retrieval")
    fake_retrieval.EmbeddingVector = object()
    fake_retrieval.M2_EMBEDDING_DIMENSIONS = 7
    monkeypatch.setitem(sys.modules, "app.models.retrieval", fake_retrieval)
    migration_path = BACKEND_ROOT / "alembic" / "versions" / "0004_retrieval.py"
    spec = importlib.util.spec_from_file_location("frozen_0004_retrieval", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.M2_EMBEDDING_DIMENSIONS == 1024


def test_embedding_vector_round_trips_compact_sqlite_json():
    from app.models.retrieval import EmbeddingVector

    metadata = MetaData()
    vectors = Table(
        "embedding_round_trip",
        metadata,
        Column("id", String(), primary_key=True),
        Column("embedding", EmbeddingVector(), nullable=False),
    )
    engine = fk_enforced_engine()
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(insert(vectors).values(id="v-1", embedding=[1, 2.5]))
        with engine.connect() as connection:
            assert connection.execute(select(vectors.c.embedding)).scalar_one() == [1.0, 2.5]
            assert connection.execute(text("SELECT embedding FROM embedding_round_trip")).scalar_one() == "[1.0,2.5]"
    finally:
        engine.dispose()


def test_sqlite_embedding_fingerprint_and_verifier_detect_wrong_type(tmp_path):
    url = f"sqlite:///{tmp_path / 'wrong-embedding-type.db'}"
    adopt(url)
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE record_embeddings")
            connection.exec_driver_sql("""
                CREATE TABLE record_embeddings (
                    id VARCHAR NOT NULL PRIMARY KEY,
                    chunk_id VARCHAR NOT NULL REFERENCES record_chunks (id) ON DELETE CASCADE,
                    model_key VARCHAR NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    CONSTRAINT uq_record_embedding_chunk_model UNIQUE (chunk_id, model_key),
                    CONSTRAINT ck_record_embedding_dimensions_m2 CHECK (dimensions = 1024)
                )
            """)
            connection.exec_driver_sql(
                "CREATE INDEX ix_record_embeddings_model_key_chunk_id "
                "ON record_embeddings (model_key, chunk_id)"
            )
        fingerprint = schema_fingerprint(engine)
        assert (
            "embedding", "BLOB", False, None
        ) in fingerprint["tables"]["record_embeddings"]["columns"]
        with engine.connect() as connection:
            assert any(
                "record_embeddings.embedding" in failure and "BLOB" in failure
                for failure in verify_revision(connection, "0004_retrieval")
            )
    finally:
        engine.dispose()


def _embedding_type(engine) -> str:
    column_type = next(
        column["type"]
        for column in inspect(engine).get_columns("record_embeddings")
        if column["name"] == "embedding"
    )
    return str(column_type).upper()


def _seed_retrieval_parents(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, google_sub, email, name) VALUES "
            "('u-1', NULL, 'u1@example.test', 'U1'), "
            "('u-2', NULL, 'u2@example.test', 'U2')"
        ))
        connection.execute(text(
            "INSERT INTO family_rooms (id, name, invite_code, owner_user_id) VALUES "
            "('r-1', 'R1', 'ROOM0001', 'u-1'), ('r-2', 'R2', 'ROOM0002', 'u-2')"
        ))
        connection.execute(text(
            "INSERT INTO uploads (id, room_id, uploader_user_id, type, title) VALUES "
            "('rec-1', 'r-1', 'u-1', 'image', 'One'), "
            "('rec-2', 'r-2', 'u-2', 'image', 'Two')"
        ))
        connection.execute(text(
            "INSERT INTO upload_files "
            "(id, upload_id, storage_bucket, storage_path, mime_type, file_size, encrypted) VALUES "
            "('f-1', 'rec-1', 'local-media', 'rec-1.png', 'image/png', 4, 0), "
            "('f-2', 'rec-2', 'local-media', 'rec-2.png', 'image/png', 4, 0)"
        ))
        connection.execute(text(
            "INSERT INTO processing_jobs "
            "(id, record_id, upload_file_id, room_id, processor, status, attempt) VALUES "
            "('job-1', 'rec-1', 'f-1', 'r-1', 'ocr', 'SUCCEEDED', 1), "
            "('job-2', 'rec-2', 'f-2', 'r-2', 'ocr', 'SUCCEEDED', 1)"
        ))
    root = Path(os.environ["AMBIENT_LEGACY_STORAGE_ROOT"])
    (root / "rec-1.png").write_bytes(b"data")
    (root / "rec-2.png").write_bytes(b"data")


def _chunk_values(
    *,
    chunk_id: str = "chunk-1",
    room_id: str = "r-1",
    record_id: str = "rec-1",
    file_id: str = "f-1",
    job_id: str = "job-1",
) -> dict:
    return {
        "id": chunk_id,
        "room_id": room_id,
        "record_id": record_id,
        "file_id": file_id,
        "processing_job_id": job_id,
        "source_attempt": 1,
        "source_result_sha256": "a" * 64,
        "ordinal": 0,
        "text": "retrieval text",
        "text_sha256": "b" * 64,
        "token_count": 2,
        "char_start": 0,
        "char_end": 14,
        "source_locator_json": '{"kind":"full_text"}',
        "extractor": "AUTO_OCR",
        "extractor_id": "test-ocr:v1",
        "review_status": "unreviewed",
        "user_confirmed": False,
        "chunker_version": "ws160-o32-v1",
    }


def test_0004_upgrades_empty_sqlite_and_registers_models(tmp_path):
    from app.core.database import Base
    from app.models import RecordChunk, RecordEmbedding

    url = f"sqlite:///{tmp_path / 'empty-0004.db'}"
    command.upgrade(_build_alembic_config(url), "0004_retrieval")
    engine = fk_enforced_engine(url)
    try:
        assert _version_rows(url) == [("0004_retrieval",)]
        assert {RecordChunk.__tablename__, RecordEmbedding.__tablename__} <= set(Base.metadata.tables)
        assert {"record_chunks", "record_embeddings"} <= set(inspect(engine).get_table_names())
        assert _embedding_type(engine) == "TEXT"
        with engine.connect() as connection:
            assert verify_revision(connection, "0004_retrieval") == []
    finally:
        engine.dispose()


def test_verified_stamped_0003_upgrades_through_0004(tmp_path):
    url = f"sqlite:///{tmp_path / 'stamped-0003.db'}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0003_processing_jobs")
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            assert verify_revision(connection, "0003_processing_jobs") == []
    finally:
        engine.dispose()

    command.upgrade(config, "0004_retrieval")
    engine = fk_enforced_engine(url)
    try:
        assert _version_rows(url) == [("0004_retrieval",)]
        with engine.connect() as connection:
            assert verify_revision(connection, "0004_retrieval") == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("damaged_table", ("processing_jobs", "deletion_intents"))
def test_0004_verifier_is_read_only_and_cumulative(tmp_path, damaged_table):
    db_path = tmp_path / "verify-0004.db"
    url = f"sqlite:///{db_path}"
    command.upgrade(_build_alembic_config(url), "0004_retrieval")
    before = sha256_of(db_path)
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            assert verify_revision(connection, "0004_retrieval") == []
            assert verify_revision(connection, "0004_retrieval") == []
        assert sha256_of(db_path) == before
        with engine.begin() as connection:
            connection.exec_driver_sql(f"DROP TABLE {damaged_table}")
        with engine.connect() as connection:
            assert any(
                damaged_table in failure
                for failure in verify_revision(connection, "0004_retrieval")
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"room_id": "r-2"},
        {"file_id": "f-2"},
        {"job_id": "job-2"},
    ],
    ids=("wrong-room", "file-from-other-record", "job-from-other-source"),
)
def test_0004_composite_foreign_keys_reject_cross_wired_provenance(tmp_path, overrides):
    url = f"sqlite:///{tmp_path / ('cross-wire-' + next(iter(overrides)) + '.db')}"
    adopt(url)
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        metadata = MetaData()
        metadata.reflect(bind=engine, only=["record_chunks"])
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(metadata.tables["record_chunks"]).values(**_chunk_values(**overrides))
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("include_embedding", "expected_counts"),
    ((False, "record_chunks=1, record_embeddings=0"), (True, "record_chunks=1, record_embeddings=1")),
    ids=("chunks-only", "chunks-and-embeddings"),
)
def test_0004_downgrade_refuses_populated_derived_tables_without_mutation(
    tmp_path, include_embedding, expected_counts
):
    db_path = tmp_path / "populated-retrieval.db"
    url = f"sqlite:///{db_path}"
    command.upgrade(_build_alembic_config(url), "0004_retrieval")
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        metadata = MetaData()
        metadata.reflect(bind=engine, only=["record_chunks", "record_embeddings"])
        with engine.begin() as connection:
            connection.execute(insert(metadata.tables["record_chunks"]).values(**_chunk_values()))
            if include_embedding:
                connection.execute(insert(metadata.tables["record_embeddings"]).values(
                    id="embedding-1",
                    chunk_id="chunk-1",
                    model_key="bge-m3:test",
                    dimensions=1024,
                    embedding="[]",
                ))
    finally:
        engine.dispose()

    before = sha256_of(db_path)
    with pytest.raises(RuntimeError) as exc_info:
        command.downgrade(_build_alembic_config(url), "0003_processing_jobs")
    assert str(exc_info.value) == f"0004 downgrade refused: {expected_counts}"
    assert sha256_of(db_path) == before
    assert _version_rows(url) == [("0004_retrieval",)]


def test_0004_empty_downgrade_removes_only_retrieval_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty-downgrade.db'}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0004_retrieval")
    command.downgrade(config, "0003_processing_jobs")
    engine = fk_enforced_engine(url)
    try:
        inspector = inspect(engine)
        assert "record_chunks" not in inspector.get_table_names()
        assert "record_embeddings" not in inspector.get_table_names()
        for table_name, columns in {
            "uploads": ("id", "room_id"),
            "upload_files": ("id", "upload_id"),
            "processing_jobs": ("id", "record_id", "upload_file_id", "room_id"),
        }.items():
            unique_sets = {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints(table_name)
            }
            assert columns not in unique_sets
    finally:
        engine.dispose()
    assert _version_rows(url) == [("0003_processing_jobs",)]

    command.upgrade(config, "0004_retrieval")
    assert _version_rows(url) == [("0004_retrieval",)]


def test_cli_check_reports_state_and_mutates_nothing(tmp_path):
    db_path = tmp_path / "cli.db"
    url = f"sqlite:///{db_path}"
    build_legacy_database(url).dispose()
    script = str(BACKEND_ROOT / "scripts" / "adopt_alembic.py")
    before = sha256_of(db_path)

    completed = subprocess.run([sys.executable, script, "--check", "--database-url", url], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "legacy"
    assert url not in completed.stdout + completed.stderr
    assert sha256_of(db_path) == before

    completed = subprocess.run([sys.executable, script, "--database-url", url], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert _version_rows(url) == [(_head_revision(),)]


def test_cli_accepts_percent_bearing_database_url_without_disclosure(tmp_path):
    db_path = tmp_path / "percent.db"
    build_legacy_database(f"sqlite:///{db_path}").dispose()
    secret = "SYNTHETIC_SECRET_R5"
    url = f"sqlite:///{db_path}?token={secret}%2Ftail"
    script = str(BACKEND_ROOT / "scripts" / "adopt_alembic.py")

    completed = subprocess.run(
        [sys.executable, script, "--database-url", url],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "legacy"
    assert secret not in completed.stdout + completed.stderr


def test_cli_failure_redacts_database_url_and_traceback(tmp_path):
    db_path = tmp_path / "cli-failure.db"
    engine = build_legacy_database(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('9999_bogus')"))
    engine.dispose()
    secret = "SYNTHETIC_SECRET_R5"
    url = f"sqlite:///{db_path}?token={secret}%2Ftail"
    script = str(BACKEND_ROOT / "scripts" / "adopt_alembic.py")

    completed = subprocess.run(
        [sys.executable, script, "--check", "--database-url", url],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr.strip() == "error: RuntimeError"
    assert secret not in completed.stdout + completed.stderr
    assert "Traceback" not in completed.stderr


def test_signup_then_downgrade_then_upgrade_round_trips(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    url = f"sqlite:///{db_path}"
    adopt(url)
    _signup(url)
    before = _identity_set(url)
    assert len(before) == 1

    config = _build_alembic_config(url)
    command.downgrade(config, "0001_baseline")

    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT google_sub FROM users")).scalar_one() == "local:alice_test"
            assert "auth_identities" not in inspect(connection).get_table_names()
            assert not [c for c in inspect(connection).get_columns("users") if c["name"] == "google_sub"][0]["nullable"]
    finally:
        engine.dispose()
    assert _fk_check_clean(url)
    assert _version_rows(url) == [("0001_baseline",)]

    command.upgrade(config, "head")
    after = _identity_set(url)
    assert {row[1:] for row in after} == {row[1:] for row in before}
    assert {row[0] for row in after} == {row[0] for row in before}
    assert _version_rows(url) == [(_head_revision(),)]

    fresh_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    adopt(fresh_url)
    round_engine, fresh_engine = fk_enforced_engine(url), fk_enforced_engine(fresh_url)
    try:
        assert schema_fingerprint(round_engine) == schema_fingerprint(fresh_engine)
    finally:
        round_engine.dispose()
        fresh_engine.dispose()


_USER = "INSERT INTO users (id, google_sub, username, password_hash, email, name) VALUES ({id}, {sub}, {user}, {hash}, {email}, 'N')"
_IDENTITY = ("INSERT INTO auth_identities (id, user_id, provider, provider_subject, credential_hash, provider_email) "
             "VALUES ({id}, {uid}, {prov}, {subj}, {hash}, NULL)")


def _sql(template, **kwargs):
    return text(template.format(**{k: "NULL" if v is None else f"'{v}'" for k, v in kwargs.items()}))


REFUSAL_FIXTURES = {
    "dual_identity": [
        _sql(_USER, id="u1", sub=None, user="dual", hash="h", email="d@example.test"),
        _sql(_IDENTITY, id="i1", uid="u1", prov="local", subj="dual", hash="h"),
        _sql(_IDENTITY, id="i2", uid="u1", prov="google", subj="g-dual", hash=None),
    ],
    "zero_identity": [
        _sql(_USER, id="u1", sub=None, user="zero", hash="h", email="z@example.test"),
    ],
    "empty_subject": [
        _sql(_USER, id="u1", sub=None, user="empty", hash="h", email="e@example.test"),
        _sql(_IDENTITY, id="i1", uid="u1", prov="local", subj="", hash="h"),
    ],
    "username_mismatch": [
        _sql(_USER, id="u1", sub=None, user="bob", hash="h", email="m@example.test"),
        _sql(_IDENTITY, id="i1", uid="u1", prov="local", subj="alice", hash="h"),
        _sql(_USER, id="u2", sub=None, user=None, hash="h", email="m2@example.test"),
        _sql(_IDENTITY, id="i2", uid="u2", prov="local", subj="carol", hash="h"),
    ],
    "password_hash_mismatch": [
        _sql(_USER, id="u1", sub=None, user="pw", hash="h-old", email="p@example.test"),
        _sql(_IDENTITY, id="i1", uid="u1", prov="local", subj="pw", hash="h-new"),
        _sql(_USER, id="u2", sub=None, user="pw2", hash=None, email="p2@example.test"),
        _sql(_IDENTITY, id="i2", uid="u2", prov="local", subj="pw2", hash="h"),
    ],
    "google_subject_local_prefix": [
        _sql(_USER, id="u1", sub=None, user="x", hash="h", email="x@example.test"),
        _sql(_IDENTITY, id="i1", uid="u1", prov="local", subj="x", hash="h"),
        _sql(_USER, id="u2", sub=None, user=None, hash=None, email="g@example.test"),
        _sql(_IDENTITY, id="i2", uid="u2", prov="google", subj="local:x", hash=None),
    ],
    "legacy_value_conflict": [
        _sql(_USER, id="u1", sub=None, user="x", hash="h", email="x@example.test"),
        _sql(_IDENTITY, id="i1", uid="u1", prov="local", subj="x", hash="h"),
        _sql(_USER, id="u2", sub="local:x", user=None, hash=None, email="g@example.test"),
        _sql(_IDENTITY, id="i2", uid="u2", prov="google", subj="g-2", hash=None),
    ],
}


@pytest.mark.parametrize("category", sorted(REFUSAL_FIXTURES))
def test_downgrade_refuses_non_round_trippable_state_without_mutation(tmp_path, category):
    db_path = tmp_path / f"{category}.db"
    url = f"sqlite:///{db_path}"
    adopt(url)
    engine = fk_enforced_engine(url)
    with engine.begin() as connection:
        for statement in REFUSAL_FIXTURES[category]:
            connection.execute(statement)
    engine.dispose()

    before = sha256_of(db_path)
    with pytest.raises(RuntimeError) as exc_info:
        command.downgrade(_build_alembic_config(url), "0001_baseline")
    message = str(exc_info.value)
    assert message.startswith("0002 downgrade refused:")
    assert f"{category}=" in message
    if category == "google_subject_local_prefix":
        assert "reconstructed_collision=1" in message
    for leaked in ("u1", "u2", "alice", "local:x", "h-old", "example.test"):
        assert leaked not in message
    assert sha256_of(db_path) == before
    assert _version_rows(url) == [(_head_revision(),)]


def _postgres_reset(url: str) -> None:
    engine = fk_enforced_engine(url)
    try:
        metadata = MetaData()
        metadata.reflect(bind=engine)
        metadata.drop_all(engine)
    finally:
        engine.dispose()


def test_signup_then_downgrade_then_upgrade_round_trips_postgres():
    url = postgres_url_or_skip()
    _postgres_reset(url)
    try:
        adopt(url)
        _signup(url)
        before = _identity_set(url)
        config = _build_alembic_config(url)
        command.downgrade(config, "0001_baseline")
        engine = fk_enforced_engine(url)
        try:
            with engine.connect() as connection:
                assert connection.execute(text("SELECT google_sub FROM users")).scalar_one() == "local:alice_test"
        finally:
            engine.dispose()
        command.upgrade(config, "head")
        assert {row[1:] for row in _identity_set(url)} == {row[1:] for row in before}
    finally:
        _postgres_reset(url)


def test_fresh_and_adopted_legacy_fingerprints_match_postgres():
    fresh_url = postgres_url_or_skip()
    legacy_url = fresh_url + "_legacy"
    _postgres_reset(fresh_url)
    _postgres_reset(legacy_url)
    try:
        adopt(fresh_url)
        build_legacy_database(legacy_url).dispose()
        adopt(legacy_url)
        fresh_engine, legacy_engine = fk_enforced_engine(fresh_url), fk_enforced_engine(legacy_url)
        try:
            fresh, legacy = schema_fingerprint(fresh_engine), schema_fingerprint(legacy_engine)
            assert fresh["dialect"] == legacy["dialect"] == "postgresql"
            assert (
                "embedding", ("vector", 1024), False, None
            ) in fresh["tables"]["record_embeddings"]["columns"]
            assert fresh == legacy
        finally:
            fresh_engine.dispose()
            legacy_engine.dispose()
    finally:
        _postgres_reset(fresh_url)
        _postgres_reset(legacy_url)


def test_stamped_head_verifies_postgres():
    url = postgres_url_or_skip()
    _postgres_reset(url)
    try:
        assert adopt(url) == "fresh"
        engine = fk_enforced_engine(url)
        try:
            with engine.connect() as connection:
                assert verify_revision(connection, "0004_retrieval") == []
        finally:
            engine.dispose()
        assert adopt(url) == "0004_retrieval"
    finally:
        _postgres_reset(url)


def test_orphaned_related_user_blocks_adoption_postgres():
    url = postgres_url_or_skip()
    _postgres_reset(url)
    try:
        engine = build_legacy_database(url)
        _seed_legacy_rows(engine)
        with engine.begin() as connection:
            connection.execute(text("UPDATE family_members SET related_to_user_id = 'u-gone' WHERE id = 'm-2'"))
        engine.dispose()
        with pytest.raises(RuntimeError, match="1 orphaned"):
            adopt(url)
        assert recorded_revisions_of(url) is None
    finally:
        _postgres_reset(url)


def test_adoption_with_rows_under_enforced_fk_postgres():
    url = postgres_url_or_skip()
    _postgres_reset(url)
    try:
        engine = build_legacy_database(url)
        _seed_legacy_rows(engine)
        engine.dispose()
        assert adopt(url) == "legacy"
        engine = fk_enforced_engine(url)
        try:
            with engine.connect() as connection:
                assert connection.execute(text("SELECT COUNT(*) FROM auth_identities")).scalar_one() == 2
            assert (("related_to_user_id",), "users", ("id",)) in schema_fingerprint(engine)["tables"]["family_members"]["foreign_keys"]
        finally:
            engine.dispose()
    finally:
        _postgres_reset(url)


def test_preflight_reports_each_pf1_to_pf5_failure(tmp_path):
    # PF-1: a baseline table is entirely missing.
    pf1_url = f"sqlite:///{tmp_path / 'pf1.db'}"
    pf1_engine = fk_enforced_engine(pf1_url)
    only_users = MetaData()
    Table("users", only_users, Column("id", String(), primary_key=True))
    only_users.create_all(pf1_engine)
    with pf1_engine.connect() as connection:
        failures = preflight(connection)
    assert any(f.startswith("PF-1:") for f in failures), failures

    # PF-2: baseline table exists but a helper-added column is missing.
    pf2_url = f"sqlite:///{tmp_path / 'pf2.db'}"
    pf2_engine = build_legacy_database(pf2_url, apply_auth_columns=False)
    with pf2_engine.connect() as connection:
        failures = preflight(connection)
    assert any(f.startswith("PF-2:") and "username" in f for f in failures), failures

    # PF-3: columns exist but the expected unique index is missing.
    pf3_url = f"sqlite:///{tmp_path / 'pf3.db'}"
    pf3_engine = build_legacy_database(pf3_url, create_username_index=False)
    with pf3_engine.connect() as connection:
        failures = preflight(connection)
    assert any(f.startswith("PF-3:") and "username" in f for f in failures), failures

    # PF-4: alembic_version stamped at an unrecognized revision.
    pf4_url = f"sqlite:///{tmp_path / 'pf4.db'}"
    pf4_engine = build_legacy_database(pf4_url)
    with pf4_engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('9999_bogus')"))
    with pf4_engine.connect() as connection:
        failures = preflight(connection)
    assert any(f.startswith("PF-4:") for f in failures), failures

    # PF-5: a NOT-NULL baseline column contains NULLs.
    pf5_url = f"sqlite:///{tmp_path / 'pf5.db'}"
    pf5_engine = build_legacy_database(pf5_url, users_email_nullable=True)
    with pf5_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, google_sub, email, name) "
                "VALUES ('user-null-email', 'local:nulluser', NULL, 'Null Email')"
            )
        )
    with pf5_engine.connect() as connection:
        failures = preflight(connection)
    assert any(f.startswith("PF-5:") and "email" in f for f in failures), failures


def test_auth_identity_backfill_maps_local_and_google_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'backfill.db'}"
    engine = build_legacy_database(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, google_sub, username, password_hash, email, name) "
                "VALUES ('user-local-1', 'local:demo_user', 'demo_user', "
                "'pbkdf2_sha256$390000$salt$hash', 'demo@example.test', 'Demo User')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, google_sub, username, password_hash, email, name) "
                "VALUES ('user-google-1', 'google-sub-XXXX', NULL, NULL, "
                "'legacy@example.test', 'Legacy Google User')"
            )
        )

    adopt(url)

    with engine.connect() as connection:
        rows = {
            row.user_id: row
            for row in connection.execute(
                text(
                    "SELECT user_id, provider, provider_subject, credential_hash, provider_email "
                    "FROM auth_identities"
                )
            ).all()
        }

    local_row = rows["user-local-1"]
    assert local_row.provider == "local"
    assert local_row.provider_subject == "demo_user"
    assert local_row.credential_hash == "pbkdf2_sha256$390000$salt$hash"
    assert local_row.provider_email == "demo@example.test"

    google_row = rows["user-google-1"]
    assert google_row.provider == "google"
    assert google_row.provider_subject == "google-sub-XXXX"
    assert google_row.credential_hash is None
    assert google_row.provider_email == "legacy@example.test"


def test_auth_identity_constraints_reject_collisions_and_bad_hash_shape(tmp_path):
    url = f"sqlite:///{tmp_path / 'constraints.db'}"
    adopt(url)
    engine = fk_enforced_engine(url)
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["users", "auth_identities"])
    users_table = metadata.tables["users"]
    auth_identities_table = metadata.tables["auth_identities"]

    with engine.begin() as connection:
        connection.execute(
            insert(users_table).values(
                id="user-1",
                google_sub=None,
                username="alice",
                password_hash="pbkdf2_sha256$390000$salt$hash",
                email="alice@example.test",
                name="Alice",
            )
        )
        connection.execute(
            insert(users_table).values(
                id="user-2",
                google_sub=None,
                username="bob",
                password_hash="pbkdf2_sha256$390000$salt$hash2",
                email="bob@example.test",
                name="Bob",
            )
        )
        connection.execute(
            insert(auth_identities_table).values(
                id="identity-1",
                user_id="user-1",
                provider="local",
                provider_subject="alice",
                credential_hash="pbkdf2_sha256$390000$salt$hash",
                provider_email="alice@example.test",
            )
        )

    # Cross-user collision on (provider, provider_subject).
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(auth_identities_table).values(
                    id="identity-2",
                    user_id="user-2",
                    provider="local",
                    provider_subject="alice",
                    credential_hash="pbkdf2_sha256$390000$salt$hash2",
                    provider_email="bob@example.test",
                )
            )

    # Per-user, per-provider collision.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(auth_identities_table).values(
                    id="identity-3",
                    user_id="user-1",
                    provider="local",
                    provider_subject="alice-second",
                    credential_hash="pbkdf2_sha256$390000$salt$hash3",
                    provider_email="alice@example.test",
                )
            )

    # CHECK: provider="local" requires a non-NULL credential_hash.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(auth_identities_table).values(
                    id="identity-4",
                    user_id="user-2",
                    provider="local",
                    provider_subject="bob",
                    credential_hash=None,
                    provider_email="bob@example.test",
                )
            )

    # CHECK: provider="google" must not carry a credential_hash.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(auth_identities_table).values(
                    id="identity-5",
                    user_id="user-2",
                    provider="google",
                    provider_subject="google-sub-bob",
                    credential_hash="unexpected-hash",
                    provider_email="bob@example.test",
                )
            )

    engine.dispose()
