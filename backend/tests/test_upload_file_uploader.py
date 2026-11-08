"""LEGACY byte-uploader persistence and frozen-SECURE ORM compatibility."""
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config
from test_processing_api import (
    IMAGE_FILE_ID, OTHER_USER_ID, RECORD_ID, ROOM_ID, USER_ID, _setup, _teardown,
)
from app.api import deps
from app.api.routes import uploads as upload_routes
from app.main import app
from app.models.family import FamilyMember
from app.models.upload import Upload, UploadFile
from app.models.user import User


def test_owner_replacements_preserve_uploader_and_deny_shared_writers(monkeypatch):
    engine, tracker, old_sl, old_ocr = _setup()
    monkeypatch.setattr(upload_routes, "SessionLocal", tracker)
    monkeypatch.setattr(
        upload_routes, "store_media_file",
        lambda content, name, mime, metadata=None, attempt_id=None: (
            "local-media",
            f"{Path(name).stem}__{attempt_id}{Path(name).suffix}",
        ),
    )
    try:
        with tracker.session_maker() as db:
            db.add(FamilyMember(
                id="fm-uploader-extra", room_id=ROOM_ID,
                user_id=OTHER_USER_ID, role="member",
            ))
            db.commit()

        client = TestClient(app)
        app.dependency_overrides[deps.get_current_user] = lambda: User(
            id=OTHER_USER_ID, google_sub=None, username="pa_other",
            password_hash="x", email="pa-other@example.test", name="PA Other",
        )
        denied = client.post(
            f"/api/v1/uploads/{RECORD_ID}/binary",
            files={"file": ("first.png", upload_routes.ONE_PIXEL_PNG + b"synthetic-one", "image/png")},
        )
        assert denied.status_code == 403, denied.text
        with tracker.session_maker() as db:
            assert db.get(UploadFile, IMAGE_FILE_ID).file_uploader_user_id is None
        app.dependency_overrides[deps.get_current_user] = lambda: User(
            id=USER_ID, google_sub=None, username="pa_user",
            password_hash="x", email="pa@example.test", name="PA User",
        )
        first = client.post(
            f"/api/v1/uploads/{RECORD_ID}/binary",
            files={"file": ("first.png", upload_routes.ONE_PIXEL_PNG + b"synthetic-one", "image/png")},
        )
        assert first.status_code == 200, first.text
        first_file_id = first.json()["file_id"]

        with tracker.session_maker() as db:
            first_file = db.get(UploadFile, first_file_id)
            assert first_file.file_uploader_user_id == USER_ID
            assert db.get(Upload, RECORD_ID).uploader_user_id == USER_ID

        second = client.post(
            f"/api/v1/uploads/{RECORD_ID}/binary",
            files={"file": ("replacement.png", upload_routes.ONE_PIXEL_PNG + b"synthetic-two", "image/png")},
        )
        assert second.status_code == 200, second.text
        second_file_id = second.json()["file_id"]
        assert second_file_id != first_file_id

        with tracker.session_maker() as db:
            assert db.get(UploadFile, first_file_id).file_uploader_user_id == USER_ID
            assert db.get(UploadFile, second_file_id).file_uploader_user_id == USER_ID
            assert db.get(UploadFile, IMAGE_FILE_ID).file_uploader_user_id is None
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_uploader_migration_preserves_historical_null_and_sets_null_on_account_delete(tmp_path):
    url = f"sqlite:///{tmp_path / 'uploader-migration.sqlite'}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0006_family_graph")
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as db:
            db.exec_driver_sql(
                "INSERT INTO users (id, google_sub, email, name) VALUES "
                "('owner', NULL, 'owner@test.invalid', 'Owner'), "
                "('byte-user', NULL, 'byte@test.invalid', 'Byte User')"
            )
            db.exec_driver_sql(
                "INSERT INTO family_rooms (id, name, invite_code, owner_user_id) "
                "VALUES ('room', 'Room', 'ROOM1', 'owner')"
            )
            db.exec_driver_sql(
                "INSERT INTO uploads (id, room_id, uploader_user_id, type, title) "
                "VALUES ('record', 'room', 'owner', 'image', 'Image')"
            )
            db.exec_driver_sql(
                "INSERT INTO upload_files (id, upload_id, storage_bucket, storage_path, mime_type) "
                "VALUES ('historical', 'record', 'legacy', 'old.png', 'image/png')"
            )

        command.upgrade(config, "0008_upload_file_uploader")
        with engine.begin() as db:
            columns = {row["name"]: row for row in inspect(db).get_columns("upload_files")}
            assert columns["file_uploader_user_id"]["nullable"] is True
            assert db.exec_driver_sql(
                "SELECT file_uploader_user_id FROM upload_files WHERE id='historical'"
            ).scalar_one() is None
            db.exec_driver_sql(
                "INSERT INTO upload_files "
                "(id, upload_id, storage_bucket, storage_path, mime_type, file_uploader_user_id) "
                "VALUES ('new-file', 'record', 'legacy', 'new.png', 'image/png', 'byte-user')"
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as db:
                db.exec_driver_sql("UPDATE upload_files SET file_uploader_user_id='missing' WHERE id='new-file'")
        with pytest.raises(RuntimeError, match="attribution would be lost"):
            command.downgrade(config, "0006_family_graph")
        with engine.begin() as db:
            assert db.exec_driver_sql("SELECT file_uploader_user_id FROM upload_files WHERE id='new-file'").scalar_one() == "byte-user"
            assert db.exec_driver_sql("SELECT count(*) FROM upload_files").scalar_one() == 2
            assert db.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0008_upload_file_uploader"
            db.exec_driver_sql("DELETE FROM users WHERE id='byte-user'")
            assert db.exec_driver_sql(
                "SELECT file_uploader_user_id FROM upload_files WHERE id='new-file'"
            ).scalar_one() is None
            foreign_keys = inspect(db).get_foreign_keys("upload_files")
            uploader_fk = next(
                row for row in foreign_keys
                if "file_uploader_user_id" in row["constrained_columns"]
            )
            assert uploader_fk["referred_table"] == "users"
            assert uploader_fk["options"]["ondelete"].upper() == "SET NULL"

        command.downgrade(config, "0006_family_graph")
        assert "file_uploader_user_id" not in {
            column["name"] for column in inspect(engine).get_columns("upload_files")
        }
    finally:
        engine.dispose()


def test_exact_0005_secure_schema_works_with_shared_upload_file_mapping(tmp_path):
    url = f"sqlite:///{tmp_path / 'secure-0005-uploader.sqlite'}"
    command.upgrade(_build_alembic_config(url), "0005_secure_catalogue")
    engine = fk_enforced_engine(url)
    try:
        assert "file_uploader_user_id" not in {
            column["name"] for column in inspect(engine).get_columns("upload_files")
        }
        with engine.begin() as db:
            db.exec_driver_sql(
                "INSERT INTO users (id, google_sub, email, name) "
                "VALUES ('owner', NULL, 'owner@test.invalid', 'Owner')"
            )
            db.exec_driver_sql(
                "INSERT INTO family_rooms (id, name, invite_code, owner_user_id) "
                "VALUES ('room', 'Room', 'ROOM1', 'owner')"
            )
            db.exec_driver_sql(
                "INSERT INTO uploads (id, room_id, uploader_user_id, type, title) "
                "VALUES ('record', 'room', 'owner', 'image', 'Image')"
            )

        with sessionmaker(bind=engine)() as db:
            db.add(UploadFile(
                id="secure-shape-file", upload_id="record",
                storage_bucket="synthetic", storage_path="synthetic.png",
                mime_type="image/png",
            ))
            db.commit()
            row = db.scalar(select(UploadFile).where(UploadFile.id == "secure-shape-file"))
            assert row is not None and row.upload_id == "record"
    finally:
        engine.dispose()


@pytest.mark.parametrize("drift", ["missing", "type", "nullable", "default", "fk", "action"])
def test_uploader_adoption_rejects_schema_drift_without_mutation(tmp_path, drift):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import sqlalchemy as sa
    from scripts.adopt_alembic import adopt, migration_engine, migration_transaction
    path = tmp_path / "drift.sqlite"
    url = f"sqlite:///{path}"
    command.upgrade(_build_alembic_config(url), "head")
    engine = migration_engine(url)
    try:
        with engine.connect() as db, migration_transaction(db):
            with Operations(MigrationContext.configure(db)).batch_alter_table("upload_files") as batch:
                if drift in ("missing", "fk", "action"):
                    batch.drop_constraint("fk_upload_files_file_uploader_user_id_users", type_="foreignkey")
                if drift == "missing":
                    batch.drop_column("file_uploader_user_id")
                elif drift == "type":
                    batch.alter_column("file_uploader_user_id", type_=sa.Integer())
                elif drift == "nullable":
                    batch.alter_column("file_uploader_user_id", nullable=False)
                elif drift == "default":
                    batch.alter_column("file_uploader_user_id", server_default="unknown")
                elif drift == "action":
                    batch.create_foreign_key("fk_upload_files_file_uploader_user_id_users", "users",
                                             ["file_uploader_user_id"], ["id"], ondelete="CASCADE")
    finally:
        engine.dispose()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="file_uploader|uploader foreign key"):
        adopt(url)
    assert path.read_bytes() == before


def test_exact_uploader_adoption_is_idempotent_and_preserves_null(tmp_path):
    from scripts.adopt_alembic import adopt
    from test_migrations import build_legacy_database, _seed_legacy_rows
    path = tmp_path / "adopt.sqlite"
    url = f"sqlite:///{path}"
    engine = build_legacy_database(url)
    _seed_legacy_rows(engine)
    engine.dispose()
    assert adopt(url) == "legacy"
    before = path.read_bytes()
    from scripts.adopt_alembic import MERGED_HEAD
    assert adopt(url, check_only=True) == MERGED_HEAD
    assert adopt(url) == MERGED_HEAD
    assert path.read_bytes() == before
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as db:
            assert db.exec_driver_sql("SELECT file_uploader_user_id FROM upload_files").all() == [(None,)]
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_uploader_migration_preserves_child_rows_fks_and_exact_secure_triggers(tmp_path):
    import sqlalchemy as sa
    from scripts.adopt_alembic import verify_revision
    from test_migrations import _seed_retrieval_parents, _chunk_values
    url = f"sqlite:///{tmp_path / 'children.sqlite'}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0004_retrieval")
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        with engine.begin() as db:
            chunks = sa.Table("record_chunks", sa.MetaData(), autoload_with=db)
            db.execute(chunks.insert().values(**_chunk_values()))
        command.upgrade(config, "0006_family_graph")
        with engine.connect() as db:
            columns = {name: [c["name"] for c in inspect(db).get_columns(name)]
                       for name in ("upload_files", "processing_jobs", "record_chunks")}
            rows = {name: db.exec_driver_sql(f"SELECT {','.join(cols)} FROM {name} ORDER BY id").all()
                    for name, cols in columns.items()}
            triggers = db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()
        for target in ("0008_upload_file_uploader", "0006_family_graph"):
            if target == "0008_upload_file_uploader":
                command.upgrade(config, target)
            else:
                command.downgrade(config, target)
            with engine.connect() as db:
                assert verify_revision(db, target) == []
                for name, cols in columns.items():
                    assert db.exec_driver_sql(f"SELECT {','.join(cols)} FROM {name} ORDER BY id").all() == rows[name]
                assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all() == triggers
                assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            with pytest.raises(IntegrityError):
                with engine.begin() as db:
                    db.exec_driver_sql("UPDATE processing_jobs SET upload_file_id='missing' WHERE id='job-1'")
    finally:
        engine.dispose()
