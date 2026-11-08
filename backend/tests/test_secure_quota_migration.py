"""Cumulative SECURE migration is explicit, drained, atomic and fail closed."""
from uuid import uuid4
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from conftest import fk_enforced_engine
from scripts.adopt_alembic import (
    _build_alembic_config, adopt, verify_revision, MERGED_HEAD,
)
from test_secure_catalogue import control_update, scope, key, envelope, generation
from app.services.secure_runtime import validate_catalogue_mode, SecureUnavailable
from test_secure_media import media_client
from test_secure_identity import identity_client
from test_secure_catalogue import catalogue

OLD = "0009_ocr_review"


def secure_catalogue(tmp_path):
    url = f"sqlite:///{tmp_path / 'quota.sqlite'}"
    command.upgrade(_build_alembic_config(url), OLD)
    engine = fk_enforced_engine(url)
    with engine.begin() as db:
        owner = scope(db)
        kid = key(db, owner, state="ACTIVE")
        oid = envelope(db, owner, kid)
        control_update(db, mode="CUTOVER")
        control_update(db, mode="SECURE")
    return engine, url, oid


def assert_old_intact(engine, oid):
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == OLD
        assert verify_revision(db, OLD) == []
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE object_id=:id"), {"id": oid}) == 1
        assert db.scalar(text("SELECT mode FROM security_control")) == "SECURE"
    assert "albums" not in inspect(engine).get_table_names()


def test_single_head_fresh_schema_and_empty_downgrade_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.sqlite'}"
    config = _build_alembic_config(url)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [MERGED_HEAD]
    assert scripts.get_revision(MERGED_HEAD).down_revision == "0015_record_privacy"
    assert scripts.get_revision("0015_record_privacy").down_revision == "0014_native_text"
    assert scripts.get_revision("0014_native_text").down_revision == "0013_secure_reconciliation"
    assert scripts.get_revision("0013_secure_reconciliation").down_revision == "0012_secure_room_storage_quota"
    assert scripts.get_revision("0012_secure_room_storage_quota").down_revision == "0011_legacy_room_storage_quota"
    command.upgrade(config, "head")
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as db:
            assert verify_revision(db, MERGED_HEAD) == []
        command.downgrade(config, "0011_legacy_room_storage_quota")
        with engine.connect() as db:
            assert verify_revision(db, "0011_legacy_room_storage_quota") == []
        command.upgrade(config, "head")
        with engine.connect() as db:
            assert verify_revision(db, MERGED_HEAD) == []
    finally:
        engine.dispose()


def test_explicit_closed_secure_upgrade_preserves_envelope_and_mode(tmp_path):
    engine, url, oid = secure_catalogue(tmp_path)
    try:
        assert adopt(url) == OLD
        assert_old_intact(engine, oid)
        assert adopt(url, target_revision=MERGED_HEAD) == OLD
        with engine.connect() as db:
            assert verify_revision(db, MERGED_HEAD) == []
            assert db.scalar(text("SELECT ciphertext_size FROM encrypted_objects WHERE object_id=:id"), {"id": oid}) == 100
            assert db.execute(text("SELECT mode,admission,runtime_generation FROM security_control")).one() == ("SECURE", "CLOSED", None)
        validate_catalogue_mode(engine, "SECURE")
        assert adopt(url, target_revision=MERGED_HEAD) == MERGED_HEAD
        with pytest.raises(RuntimeError, match="closed LEGACY"):
            command.downgrade(_build_alembic_config(url), "0011_legacy_room_storage_quota")
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == MERGED_HEAD
    finally:
        engine.dispose()


@pytest.mark.parametrize("problem", ["no-opt-in", "open", "live-generation", "populated-graph", "drift"])
def test_secure_bridge_refuses_unsafe_state_without_mutation(tmp_path, problem):
    engine, url, oid = secure_catalogue(tmp_path)
    try:
        with engine.begin() as db:
            if problem == "open":
                gid = generation(db)
                db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=2,invocation_id=:i,boot_id=:b WHERE generation_id=:g"),
                           {"i": str(uuid4()), "b": str(uuid4()), "g": gid})
                control_update(db, runtime_generation=gid)
                control_update(db, admission="OPEN")
            elif problem == "live-generation":
                generation(db)
            elif problem == "populated-graph":
                db.exec_driver_sql("INSERT INTO users(id) VALUES ('graph-user')")
                db.exec_driver_sql("INSERT INTO family_rooms(id,owner_user_id) VALUES ('graph-room','graph-user')")
                db.exec_driver_sql("INSERT INTO family_people(id,room_id,display_name) VALUES ('person','graph-room','plaintext')")
            elif problem == "drift":
                db.exec_driver_sql("DROP TRIGGER m4_operation_transition")
        with pytest.raises(RuntimeError):
            if problem == "no-opt-in":
                command.upgrade(_build_alembic_config(url), MERGED_HEAD)
            else:
                adopt(url, target_revision=MERGED_HEAD)
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == OLD
            assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE object_id=:id"), {"id": oid}) == 1
        assert "albums" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_later_migration_failure_rolls_back_entire_secure_bridge(tmp_path, monkeypatch):
    engine, url, oid = secure_catalogue(tmp_path)
    root = tmp_path / "untracked-storage"
    root.mkdir()
    (root / "untracked.bin").write_bytes(b"synthetic")
    monkeypatch.setenv("AMBIENT_LEGACY_STORAGE_ROOT", str(root))
    monkeypatch.setenv("AMBIENT_LEGACY_STORAGE_BACKFILL_APPROVED", "1")
    try:
        with pytest.raises(RuntimeError, match="untracked physical"):
            adopt(url, target_revision=MERGED_HEAD)
        assert_old_intact(engine, oid)
    finally:
        engine.dispose()


@pytest.mark.parametrize("trigger", ["m4_operation_quota_insert", "quota_family_people_insert", "album_legacy_cutover_fence",
    "secure_create_bindings_update", "secure_reconciliation_events_delete"])
def test_new_schema_verifier_rejects_missing_safety_fences(tmp_path, trigger):
    engine, url, _ = secure_catalogue(tmp_path)
    try:
        adopt(url, target_revision=MERGED_HEAD)
        with engine.begin() as db:
            db.exec_driver_sql("DROP TRIGGER " + trigger)
        with pytest.raises(SecureUnavailable):
            validate_catalogue_mode(engine, "SECURE")
    finally:
        engine.dispose()


def test_explicit_alembic_flag_accepts_head_alias_only_for_exact_0009(tmp_path):
    engine, url, _ = secure_catalogue(tmp_path)
    try:
        config = _build_alembic_config(url)
        config.cmd_opts = SimpleNamespace(x=["secure_quota_upgrade=1"])
        command.upgrade(config, "head")
        with engine.connect() as db:
            assert verify_revision(db, MERGED_HEAD) == []
    finally:
        engine.dispose()


def test_plaintext_graph_blocks_cutover_and_cannot_enter_secure_schema(tmp_path):
    from sqlalchemy.exc import IntegrityError
    url = f"sqlite:///{tmp_path / 'graph-fences.sqlite'}"
    command.upgrade(_build_alembic_config(url), MERGED_HEAD)
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as db:
            db.exec_driver_sql("INSERT INTO users(id) VALUES ('graph-user')")
            db.exec_driver_sql("INSERT INTO family_rooms(id,owner_user_id) VALUES ('graph-room','graph-user')")
            db.exec_driver_sql("INSERT INTO family_people(id,room_id,display_name) VALUES ('person','graph-room','synthetic')")
        with engine.begin() as db:
            with pytest.raises(IntegrityError, match="plaintext extension"):
                control_update(db, mode="CUTOVER")
        with engine.begin() as db:
            db.exec_driver_sql("DELETE FROM family_people")
            control_update(db, mode="CUTOVER")
            control_update(db, mode="SECURE")
        with engine.begin() as db:
            with pytest.raises(IntegrityError, match="family graph requires LEGACY"):
                db.exec_driver_sql("INSERT INTO family_people(id,room_id,display_name) VALUES ('person','graph-room','synthetic')")
    finally:
        engine.dispose()


@pytest.mark.parametrize("assignment", ["", "reserved_ciphertext_bytes=NULL,", "reserved_ciphertext_bytes=0,", "reserved_ciphertext_bytes=-1,"])
def test_claim_revision_cannot_advance_without_positive_reservation(media_client, assignment):
    from sqlalchemy.exc import IntegrityError
    from test_secure_storage_quota import quota_claim, record
    c = media_client
    saved, _ = quota_claim(c, record(c))
    with pytest.raises(IntegrityError), c.engine.begin() as db:
        db.execute(text("UPDATE secure_operations SET " + assignment + "revision=revision+1 WHERE operation_id=:id"),
                   {"id": str(saved.operation_id)})
    with c.engine.connect() as db:
        assert db.execute(text("SELECT state,revision,reserved_ciphertext_bytes FROM secure_operations WHERE operation_id=:id"),
                          {"id": str(saved.operation_id)}).one() == ("CLAIMED", 1, None)
