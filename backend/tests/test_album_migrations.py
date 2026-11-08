"""Disposable SQLite evidence for the LEGACY album / frozen SECURE boundary."""
from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import Table, inspect, text
from sqlalchemy.exc import IntegrityError

from conftest import fk_enforced_engine
from scripts.adopt_alembic import _album_migration, _build_alembic_config, adopt, verify_revision
from test_migrations import schema_fingerprint


@pytest.fixture
def legacy0005(tmp_path):
    url = f"sqlite:///{tmp_path / 'albums.sqlite'}"
    adopt(url, target_revision="0005_secure_catalogue")
    engine = fk_enforced_engine(url)
    try:
        yield engine, url, _build_alembic_config(url)
    finally:
        engine.dispose()


def seed_records(engine):
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id,name) VALUES ('u','member')"))
        db.execute(text("INSERT INTO auth_identities(id,user_id,provider,provider_subject,credential_hash) VALUES ('i','u','local','test-user','test-hash')"))
        for room in ("room-a", "room-b"):
            db.execute(text("INSERT INTO family_rooms(id,owner_user_id,name) VALUES (:id,'u',:id)"), {"id": room})
            db.execute(text("INSERT INTO uploads(id,room_id,uploader_user_id,type,title,status) VALUES (:id,:room,'u','photo',:id,'uploaded')"), {"id": "photo-" + room, "room": room})
            db.execute(text("INSERT INTO upload_files(id,upload_id,encrypted,storage_bucket,storage_path,mime_type) VALUES (:id,:upload,0,'local-media',:path,'image/png')"), {"id": "file-" + room, "upload": "photo-" + room, "path": "photo-" + room + ".png"})


def insert_album(db, album_id, room_id, **changes):
    values = {"id": album_id, "room_id": room_id, "parent_album_id": None, "cover_upload_id": None,
              "created_by": "u", "title": album_id, "tags": "[]", "sort_order": 0, "version": 1}
    values.update(changes)
    db.execute(text("INSERT INTO albums(" + ",".join(values) + ") VALUES (" + ",".join(":" + name for name in values) + ")"), values)


def test_fresh_legacy_schema_is_verified_and_has_no_implicit_albums(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.sqlite'}"
    assert adopt(url, target_revision="0007_video_albums_reads") == "fresh"
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as db:
            assert verify_revision(db, "0007_video_albums_reads") == []
            assert db.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0007_video_albums_reads"
            for name in _album_migration().ALBUM_TABLES:
                assert db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one() == 0
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    assert adopt(url, check_only=True) == "0007_video_albums_reads"


def test_existing_records_preserved_unclassified_after_upgrade(legacy0005):
    engine, url, config = legacy0005
    seed_records(engine)
    with engine.connect() as db:
        before = {name: db.execute(text(f"SELECT * FROM {name} ORDER BY id")).all()
                  for name in ("uploads", "upload_files", "family_rooms", "users", "auth_identities")}
    command.upgrade(config, "0007_video_albums_reads")
    with engine.connect() as db:
        assert verify_revision(db, "0007_video_albums_reads") == []
        for name, rows in before.items():
            assert db.execute(text(f"SELECT * FROM {name} ORDER BY id")).all() == rows
        assert db.execute(text("SELECT COUNT(*) FROM album_photos")).scalar_one() == 0
    assert adopt(url, target_revision="0007_video_albums_reads") == "0007_video_albums_reads"


@pytest.mark.parametrize("mode", ["CUTOVER", "SECURE", "QUARANTINED"])
def test_nonlegacy_catalogue_never_upgrades_and_adoption_preserves_revision(legacy0005, mode):
    engine, url, config = legacy0005
    with engine.begin() as db:
        if mode == "SECURE":
            db.execute(text("UPDATE security_control SET mode='CUTOVER',revision=revision+1"))
        db.execute(text("UPDATE security_control SET mode=:mode,revision=revision+1"), {"mode": mode})
    before = schema_fingerprint(engine)
    with pytest.raises(RuntimeError, match="refused"):
        command.upgrade(config, "0007_video_albums_reads")
    assert schema_fingerprint(engine) == before
    assert adopt(url) == "0005_secure_catalogue"
    with pytest.raises(RuntimeError, match="non-LEGACY"):
        adopt(url, target_revision="0006_photo_albums")
    with engine.connect() as db:
        assert db.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0005_secure_catalogue"
        assert verify_revision(db, "0005_secure_catalogue") == []
    if mode == "SECURE":
        from app.services.secure_runtime import validate_catalogue_mode
        validate_catalogue_mode(engine, "SECURE")


@pytest.mark.parametrize("drift", ["missing_control", "missing_fence", "partial_album", "lifecycle_data"])
def test_ambiguous_catalogue_refused_before_first_ddl(legacy0005, drift):
    engine, _, config = legacy0005
    with engine.begin() as db:
        if drift == "missing_control":
            db.exec_driver_sql("DROP TRIGGER m4_control_delete")
            db.exec_driver_sql("DELETE FROM security_control")
        elif drift == "missing_fence":
            db.exec_driver_sql("DROP TRIGGER m4_control_transition")
        elif drift == "partial_album":
            db.exec_driver_sql("CREATE TABLE albums(id TEXT PRIMARY KEY)")
        else:
            db.execute(text("INSERT INTO security_scopes(domain,owner_id,state,deletion_epoch,authorization_epoch,revision) VALUES ('room','r','ACTIVE',0,0,1)"))
    before = schema_fingerprint(engine)
    with pytest.raises(RuntimeError, match="refused"):
        command.upgrade(config, "0007_video_albums_reads")
    assert schema_fingerprint(engine) == before
    assert "album_photos" not in inspect(engine).get_table_names()


def test_interrupted_album_ddl_rolls_back_every_new_table(legacy0005, monkeypatch):
    engine, _, config = legacy0005
    before = schema_fingerprint(engine)
    original_create = Table.create

    def interrupt_create(table, bind, **kwargs):
        if table.name == "album_photos":
            raise RuntimeError("synthetic migration interruption")
        return original_create(table, bind, **kwargs)

    monkeypatch.setattr(Table, "create", interrupt_create)
    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        command.upgrade(config, "0007_video_albums_reads")
    assert schema_fingerprint(engine) == before
    with engine.connect() as db:
        assert verify_revision(db, "0005_secure_catalogue") == []


def test_0006_prevents_unreviewed_secure_cutover(legacy0005):
    engine, _, config = legacy0005
    command.upgrade(config, "0007_video_albums_reads")
    with engine.begin() as db:
        with pytest.raises(IntegrityError, match="album catalogue requires LEGACY"):
            db.execute(text("UPDATE security_control SET mode='CUTOVER',revision=revision+1"))
    from app.services.secure_runtime import SecureUnavailable, validate_catalogue_mode
    validate_catalogue_mode(engine, "LEGACY")
    with pytest.raises(SecureUnavailable):
        validate_catalogue_mode(engine, "SECURE")


def test_cross_room_relations_and_duplicate_membership_rejected_by_db(legacy0005):
    engine, _, config = legacy0005
    seed_records(engine)
    command.upgrade(config, "0007_video_albums_reads")
    with engine.begin() as db:
        insert_album(db, "a", "room-a")
        insert_album(db, "b", "room-b")
        db.execute(text("INSERT INTO album_photos(album_id,upload_id,room_id,sort_order) VALUES ('a','photo-room-a','room-a',0)"))
    for statement in (
        "UPDATE albums SET parent_album_id='b' WHERE id='a'",
        "UPDATE albums SET parent_album_id='a' WHERE id='a'",
        "UPDATE albums SET cover_upload_id='photo-room-b' WHERE id='a'",
        "INSERT INTO album_photos(album_id,upload_id,room_id,sort_order) VALUES ('a','photo-room-b','room-a',0)",
        "INSERT INTO album_photos(album_id,upload_id,room_id,sort_order) VALUES ('a','photo-room-a','room-a',0)",
        "DELETE FROM uploads WHERE id='photo-room-a'",
        "INSERT INTO photo_upload_batches(id,room_id,created_by,album_id,request_id,request_hash) VALUES ('batch','room-a','u','b','request','hash')",
    ):
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(text(statement))
    with engine.connect() as db:
        assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize("drift", ["index", "trigger", "column"])
def test_album_adoption_detects_exact_schema_drift(legacy0005, drift):
    engine, url, config = legacy0005
    command.upgrade(config, "0007_video_albums_reads")
    with engine.begin() as db:
        db.exec_driver_sql({
            "index": "DROP INDEX ix_album_photos_upload",
            "trigger": "DROP TRIGGER album_legacy_cutover_fence",
            "column": "ALTER TABLE albums ADD COLUMN unexplained TEXT",
        }[drift])
    with pytest.raises(RuntimeError, match="does not match"):
        adopt(url, check_only=True)


def test_downgrade_empty_roundtrip_and_populated_refusal(legacy0005):
    engine, _, config = legacy0005
    command.upgrade(config, "0007_video_albums_reads")
    command.downgrade(config, "0005_secure_catalogue")
    with engine.connect() as db:
        assert verify_revision(db, "0005_secure_catalogue") == []
    seed_records(engine)
    command.upgrade(config, "0007_video_albums_reads")
    with engine.begin() as db:
        insert_album(db, "a", "room-a")
    before = schema_fingerprint(engine)
    with pytest.raises(RuntimeError, match="not empty"):
        command.downgrade(config, "0005_secure_catalogue")
    assert schema_fingerprint(engine) == before
