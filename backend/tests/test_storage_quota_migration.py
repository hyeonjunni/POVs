"""0011 schema, trigger preservation, gated backfill, and downgrade evidence."""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config, recorded_revisions, MERGED_HEAD

REVISION = "0011_legacy_room_storage_quota"
PARENT = "0010_merge_album_family"


def _configure_backfill(monkeypatch, root: Path) -> None:
    root.mkdir()
    monkeypatch.setenv("AMBIENT_LEGACY_STORAGE_BACKFILL_APPROVED", "1")
    monkeypatch.setenv("AMBIENT_LEGACY_STORAGE_ROOT", str(root.resolve()))


def _upgrade_to_parent(url: str) -> None:
    command.upgrade(_build_alembic_config(url), PARENT)


def _seed_local_file_row(
    engine, *, upload_id="quota-backfill", path="quota-backfill.png", size=4,
    file_id="quota-file",
) -> None:
    with engine.begin() as db:
        if db.scalar(text("SELECT COUNT(*) FROM users")) == 0:
            db.execute(text(
                "INSERT INTO users(id,email,name) "
                "VALUES ('quota-user','quota@example.test','Quota')"
            ))
            db.execute(text(
                "INSERT INTO family_rooms(id,name,invite_code,owner_user_id) "
                "VALUES ('quota-room','Quota','QUOTA001','quota-user')"
            ))
        db.execute(text(
            "INSERT INTO uploads(id,room_id,uploader_user_id,type,title,status) "
            "VALUES (:id,'quota-room','quota-user','image',:id,'uploaded')"
        ), {"id": upload_id})
        db.execute(text(
            "INSERT INTO upload_files"
            "(id,upload_id,storage_bucket,storage_path,mime_type,file_size,encrypted) "
            "VALUES (:file,:upload,'local-media',:path,'image/png',:size,0)"
        ), {"file": file_id, "upload": upload_id, "path": path, "size": size})


def _trigger_sql(engine):
    with engine.connect() as db:
        return db.execute(text(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='upload_files' AND name LIKE 'm4_upload_files_%' "
            "ORDER BY name"
        )).all()


def test_0011_is_ancestor_of_the_only_head_and_direct_child():
    scripts = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    assert scripts.get_heads() == [MERGED_HEAD]
    assert scripts.get_revision(MERGED_HEAD).down_revision == "0015_record_privacy"
    assert scripts.get_revision("0015_record_privacy").down_revision == "0014_native_text"
    assert scripts.get_revision("0014_native_text").down_revision == "0013_secure_reconciliation"
    assert scripts.get_revision("0013_secure_reconciliation").down_revision == "0012_secure_room_storage_quota"
    assert len(MERGED_HEAD) <= 32
    assert scripts.get_revision("0012_secure_room_storage_quota").down_revision == REVISION
    assert scripts.get_revision(REVISION).down_revision == PARENT


def test_fresh_upgrade_has_quota_schema_and_empty_downgrade_roundtrip(
    tmp_path, monkeypatch,
):
    url = f"sqlite:///{tmp_path / 'fresh.sqlite'}"
    root = tmp_path / "storage"
    _configure_backfill(monkeypatch, root)
    config = _build_alembic_config(url)
    command.upgrade(config, "head")
    engine = fk_enforced_engine(url)
    try:
        schema = inspect(engine)
        assert "legacy_storage_allocations" in schema.get_table_names()
        assert "storage_allocation_id" in {
            column["name"] for column in schema.get_columns("upload_files")
        }
        command.downgrade(config, PARENT)
        assert "legacy_storage_allocations" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        with engine.connect() as db:
            assert recorded_revisions(db) == [MERGED_HEAD]
    finally:
        engine.dispose()


def test_sqlite_upload_file_immutability_triggers_are_exact_and_enforced(
    tmp_path, monkeypatch,
):
    url = f"sqlite:///{tmp_path / 'triggers.sqlite'}"
    root = tmp_path / "storage"
    _configure_backfill(monkeypatch, root)
    _upgrade_to_parent(url)
    engine = fk_enforced_engine(url)
    try:
        before = _trigger_sql(engine)
        assert {name for name, _ in before} == {
            "m4_upload_files_delete",
            "m4_upload_files_plain_insert",
            "m4_upload_files_plain_update",
        }
        command.upgrade(_build_alembic_config(url), REVISION)
        assert _trigger_sql(engine) == before
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO users(id,email,name) "
                "VALUES ('trigger-user','trigger@example.test','Trigger')"
            ))
            db.execute(text(
                "INSERT INTO family_rooms(id,name,invite_code,owner_user_id) "
                "VALUES ('trigger-room','Trigger','TRIGGER1','trigger-user')"
            ))
            db.execute(text(
                "INSERT INTO uploads(id,room_id,uploader_user_id,type,title,status) "
                "VALUES ('trigger-upload','trigger-room','trigger-user','image','T','uploaded')"
            ))
            db.exec_driver_sql("DROP TRIGGER album_legacy_cutover_fence")
            db.execute(text(
                "UPDATE security_control SET mode='CUTOVER',revision=revision+1 "
                "WHERE id=1"
            ))
        with pytest.raises(IntegrityError, match="rejects plaintext writer"):
            with engine.begin() as db:
                db.execute(text(
                    "INSERT INTO upload_files"
                    "(id,upload_id,storage_bucket,storage_path,mime_type,encrypted) "
                    "VALUES ('trigger-file','trigger-upload','local-media',"
                    "'trigger-upload.png','image/png',0)"
                ))
    finally:
        engine.dispose()


def test_gated_backfill_links_exact_real_file(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'backfill.sqlite'}"
    root = tmp_path / "storage"
    _configure_backfill(monkeypatch, root)
    _upgrade_to_parent(url)
    engine = fk_enforced_engine(url)
    try:
        _seed_local_file_row(engine)
        (root / "quota-backfill.png").write_bytes(b"data")
        command.upgrade(_build_alembic_config(url), REVISION)
        with engine.connect() as db:
            allocation = db.execute(text(
                "SELECT room_id,upload_id,storage_bucket,storage_path,byte_size,state "
                "FROM legacy_storage_allocations"
            )).one()
            link = db.scalar(text(
                "SELECT storage_allocation_id FROM upload_files "
                "WHERE id='quota-file'"
            ))
            assert allocation == (
                "quota-room", "quota-backfill", "local-media",
                "quota-backfill.png", 4, "ACTIVE",
            )
            assert link is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "problem,message",
    [
        ("missing", "tracked local file missing"),
        ("size", "exact local size mismatch"),
        ("invalid", "invalid local locator"),
        ("duplicate", "duplicate local locator"),
        ("untracked", "untracked physical local file"),
        ("intent", "nonterminal local deletion intent"),
    ],
)
def test_backfill_preflight_fails_closed_without_schema_advance(
    tmp_path, monkeypatch, problem, message,
):
    url = f"sqlite:///{tmp_path / (problem + '.sqlite')}"
    root = tmp_path / (problem + "-storage")
    _configure_backfill(monkeypatch, root)
    _upgrade_to_parent(url)
    engine = fk_enforced_engine(url)
    try:
        path = "../invalid.png" if problem == "invalid" else "quota-backfill.png"
        _seed_local_file_row(engine, path=path)
        if problem not in ("missing", "invalid"):
            (root / "quota-backfill.png").write_bytes(
                b"wrong" if problem == "size" else b"data"
            )
        if problem == "duplicate":
            _seed_local_file_row(
                engine, upload_id="quota-backfill-2",
                path="quota-backfill.png", file_id="quota-file-2",
            )
        elif problem == "untracked":
            (root / "orphan.png").write_bytes(b"orphan")
        elif problem == "intent":
            with engine.begin() as db:
                db.execute(text(
                    "INSERT INTO deletion_intents"
                    "(id,scope,scope_id,storage_bucket,storage_path,status) "
                    "VALUES ('intent','record','quota-backfill','local-media',"
                    "'quota-backfill.png','pending')"
                ))

        with pytest.raises(RuntimeError, match=message):
            command.upgrade(_build_alembic_config(url), REVISION)
        with engine.connect() as db:
            assert recorded_revisions(db) == [PARENT]
        assert "legacy_storage_allocations" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_backfill_requires_explicit_operator_gate(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'ungated.sqlite'}"
    root = tmp_path / "storage"
    root.mkdir()
    _upgrade_to_parent(url)
    engine = fk_enforced_engine(url)
    try:
        _seed_local_file_row(engine)
        (root / "quota-backfill.png").write_bytes(b"data")
        monkeypatch.setenv("AMBIENT_LEGACY_STORAGE_ROOT", str(root.resolve()))
        monkeypatch.delenv("AMBIENT_LEGACY_STORAGE_BACKFILL_APPROVED", raising=False)
        with pytest.raises(RuntimeError, match="backfill refused"):
            command.upgrade(_build_alembic_config(url), REVISION)
        with engine.connect() as db:
            assert recorded_revisions(db) == [PARENT]
    finally:
        engine.dispose()


def test_downgrade_refuses_nonempty_allocation_ledger(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'downgrade.sqlite'}"
    root = tmp_path / "storage"
    _configure_backfill(monkeypatch, root)
    config = _build_alembic_config(url)
    command.upgrade(config, REVISION)
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO legacy_storage_allocations"
                "(id,room_id,upload_id,storage_bucket,storage_path,byte_size,state,"
                "reserved_at,expires_at,updated_at) VALUES "
                "('allocation','room','upload','local-media','upload.png',1,'ACTIVE',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ))
        with pytest.raises(RuntimeError, match="ledger is not empty"):
            command.downgrade(config, PARENT)
        with engine.connect() as db:
            assert recorded_revisions(db) == [REVISION]
    finally:
        engine.dispose()
