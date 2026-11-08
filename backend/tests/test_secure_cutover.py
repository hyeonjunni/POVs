"""B6 synthetic resume contract. No production rehearsal implementation is added."""
import importlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_secure_catalogue import catalogue, service_case, put, control_update
from app.services import secure_catalogue as cat
from app.services.secure_runtime import SecureRuntime, validate_catalogue_mode, SecureUnavailable
from app.storage.contracts import KeyRef
from test_secure_runtime import FakeLock
from b6_synthetic import entry, RecordingStore, drained_authority


@pytest.fixture
def cutover_case(catalogue):
    engine, _, path = catalogue
    root = path.parent
    root.chmod(0o700)
    (root / ".ambient-m4-synthetic").write_text("B6 disposable fixture v1\n")
    first, second = str(uuid4()), str(uuid4())
    with engine.begin() as db:
        put(db, "users", id=first, name="synthetic retained one", email="one@example.com")
        put(db, "users", id=second, name="synthetic retained two", email="two@example.com")
        control_update(db, mode="CUTOVER")
        control = dict(db.execute(text("SELECT * FROM security_control")).mappings().one())
    # Retained outside the catalogue; never regenerate from a restored database.
    checkpoint = dict(catalogue_id=control["catalogue_id"], control_revision=control["revision"],
                      inventory_revisions=[], deletion_epochs=[])
    (root / "synthetic-inventory.json").write_text(json.dumps({
        "version": 1, "catalogue_id": control["catalogue_id"],
        "targets": [{"owner_type": "user", "owner_id": uid} for uid in (first, second)],
    }))
    return dict(engine=engine, root=root, checkpoint=checkpoint, users=(first, second), store=RecordingStore())


def resume(case, checkpoint):
    run = entry("scripts.rehearse_secure_cutover", "resume_synthetic_cutover")
    return run(root=case["root"], engine=case["engine"], store=case["store"], checkpoint=checkpoint)


def state(case):
    with case["engine"].connect() as db:
        return tuple(db.execute(text("SELECT mode,admission FROM security_control")).one())


def test_interrupted_cutover_resumes_closed(cutover_case):
    c = cutover_case
    assert state(c) == ("CUTOVER", "CLOSED")
    resume(c, c["checkpoint"])
    assert state(c) == ("CUTOVER", "CLOSED")
    resume(c, c["checkpoint"])
    assert state(c) == ("CUTOVER", "CLOSED")
    assert c["store"].delete_calls == []
    with pytest.raises(SecureUnavailable):
        validate_catalogue_mode(c["engine"], "LEGACY")


def test_partial_cleanup_never_reopens_plaintext(cutover_case):
    c = cutover_case
    with c["engine"].begin() as db:
        # Durable partial nulling is arranged, not an authorized cleanup runner.
        db.execute(text("UPDATE users SET name=NULL,email=NULL WHERE id=:u"), dict(u=c["users"][0]))
    resume(c, c["checkpoint"])
    assert state(c) == ("CUTOVER", "CLOSED")
    with c["engine"].connect() as db:
        assert db.scalar(text("SELECT name FROM users WHERE id=:u"), dict(u=c["users"][0])) is None
        assert db.scalar(text("SELECT name FROM users WHERE id=:u"), dict(u=c["users"][1])) == "synthetic retained two"
    with pytest.raises(SecureUnavailable):
        validate_catalogue_mode(c["engine"], "LEGACY")
    assert c["store"].delete_calls == []


@pytest.mark.parametrize("mismatch", ["missing", "catalogue", "control", "inventory", "tombstone"])
def test_stale_restored_catalogue_becomes_quarantined(cutover_case, mismatch):
    c = cutover_case
    checkpoint = dict(c["checkpoint"])
    if mismatch == "missing":
        checkpoint = None
    elif mismatch == "catalogue":
        checkpoint["catalogue_id"] = str(uuid4())
    elif mismatch == "control":
        checkpoint["control_revision"] += 1  # The restored DB is older.
    elif mismatch == "inventory":
        checkpoint["inventory_revisions"] = [str(uuid4())]
    else:
        checkpoint["deletion_epochs"] = [["user", c["users"][0], 1]]
    resume(c, checkpoint)
    assert state(c) == ("QUARANTINED", "CLOSED")
    assert c["store"].delete_calls == []
    with c["engine"].begin() as db:
        with pytest.raises(IntegrityError):
            control_update(db, mode="LEGACY")


def test_unknown_object_is_never_blind_deleted(service_case):
    c = service_case
    candidate, operation, revision = uuid4(), uuid4(), uuid4()
    with c["engine"].begin() as db:
        saved = cat.snapshot(db, principal_id=c["principal"], targets=(("room", c["room"]),))
        cat.claim(db, saved, operation_id=operation, target=("room", c["room"]), candidate_object_id=candidate)
        cat.abandon(db, operation_id=operation, expected_revision=1, receipt_unknown=True)
        control_update(db, admission="CLOSED")
        db.execute(text("UPDATE security_scopes SET deletion_epoch=1,revision=revision+1 WHERE domain='room'"))
        put(db, "security_tombstones", domain="room", owner_id=str(c["room"]), target_kind="room",
            target_id=str(c["room"]), deletion_epoch=1, revision=1)
        # A real current inventory exists. Rejection must not just mean unknown UUID.
        put(db, "secure_deletion_inventory", id=str(uuid4()), domain="room", owner_id=str(c["room"]),
            target_kind="room", target_id=str(c["room"]), deletion_epoch=1, revision=1,
            object_id=str(candidate), object_version=1, key_id=str(c["key"]), key_epoch=1,
            inventory_revision=str(revision), logical_state="LOGICALLY_REVOKED", key_state="PENDING",
            physical_state="BLOCKED", retention_state="UNKNOWN", backup_state="UNKNOWN",
            crypto_state="CRYPTO_ERASURE_NOT_ESTABLISHED", failure_code="receipt_unknown")
        ref = KeyRef("room", c["room"], c["key"], 1)
        runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
        approval, proof, supervisor = drained_authority(
            db, ref=ref, inventory_revision=revision, runtime=runtime)
    store = RecordingStore()
    cleanup = entry("app.services.secure_deletion", "cleanup_inventory")
    with pytest.raises(cat.SecureStateChanged):
        cleanup(c["engine"], store, ref=ref, approval=approval, drain_proof=proof,
                supervisor=supervisor, runtime=runtime, inventory_revision=revision)
    assert store.delete_calls == []
    with c["engine"].connect() as db:
        assert tuple(db.execute(text("SELECT state,receipt_json FROM secure_operations WHERE operation_id=:o"), dict(o=str(operation))).one()) == ("ORPHAN_UNRESOLVED", None)
        assert db.scalar(text("SELECT admission FROM security_control")) == "CLOSED"


def test_resume_requires_explicit_synthetic_marker(cutover_case):
    c = cutover_case
    (c["root"] / ".ambient-m4-synthetic").unlink()
    with pytest.raises(ValueError):
        resume(c, c["checkpoint"])
    assert state(c) == ("CUTOVER", "CLOSED")
    assert c["store"].delete_calls == []



def test_spoofed_control_trigger_is_rejected(cutover_case):
    c = cutover_case
    with c["engine"].begin() as db:
        db.execute(text("DROP TRIGGER m4_control_transition"))
        db.execute(text(
            "CREATE TRIGGER m4_control_transition BEFORE UPDATE ON security_control BEGIN SELECT 1; END"
        ))
    with pytest.raises(ValueError):
        resume(c, c["checkpoint"])
    assert state(c) == ("CUTOVER", "CLOSED")


def test_lookalike_control_table_is_rejected(cutover_case):
    c = cutover_case
    with c["engine"].begin() as db:
        db.execute(text("ALTER TABLE security_control ADD COLUMN lookalike TEXT"))
    with pytest.raises(ValueError):
        resume(c, c["checkpoint"])
    assert state(c) == ("CUTOVER", "CLOSED")


def _on_disk_state(database_path):
    with sqlite3.connect(database_path) as database:
        return tuple(database.execute("SELECT mode,admission FROM security_control").fetchone())


def test_root_swap_cannot_redirect_quarantine(cutover_case, monkeypatch):
    c = cutover_case
    root = c["root"]
    original = root.with_name(root.name + "-original")
    attacker = root.with_name(root.name + "-attacker")
    shutil.copytree(root, attacker)
    module = importlib.import_module("scripts.rehearse_secure_cutover")
    actual_open = module.os.open
    swapped = False

    def open_after_swap(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = actual_open(path, flags, *args, **kwargs)
        if not swapped and Path(path) == root:
            root.rename(original)
            attacker.rename(root)
            swapped = True
        return descriptor

    monkeypatch.setattr(module.os, "open", open_after_swap)
    resume(c, None)
    assert swapped
    assert _on_disk_state(original / "catalogue.sqlite") == ("QUARANTINED", "CLOSED")
    assert _on_disk_state(root / "catalogue.sqlite") == ("CUTOVER", "CLOSED")


def test_catalogue_swap_cannot_redirect_quarantine(cutover_case, monkeypatch):
    c = cutover_case
    root = c["root"]
    original = root / "catalogue-original.sqlite"
    attacker = root / "catalogue-attacker.sqlite"
    shutil.copy2(root / "catalogue.sqlite", attacker)
    module = importlib.import_module("scripts.rehearse_secure_cutover")
    actual_connect = module.sqlite3.connect
    swapped = False

    def connect_after_swap(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            (root / "catalogue.sqlite").rename(original)
            attacker.rename(root / "catalogue.sqlite")
            swapped = True
        return actual_connect(*args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect_after_swap)
    resume(c, None)
    monkeypatch.undo()
    assert swapped
    assert _on_disk_state(original) == ("QUARANTINED", "CLOSED")
    assert _on_disk_state(root / "catalogue.sqlite") == ("CUTOVER", "CLOSED")
