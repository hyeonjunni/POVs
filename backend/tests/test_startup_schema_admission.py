"""Real Linux application lifespan; disposable catalogues and generated credentials only."""
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config
from test_secure_catalogue import control_update, generation, key, put, scope
from app.services.secure_runtime import SecureUnavailable, validate_catalogue_mode
from app.storage.contracts import KeyRef
from app.storage.key_vault import SystemdKeyVault


def provision_startup(root, revision="0009_ocr_review"):
    """Synthetic external provisioning; no runtime, lock or vault substitution."""
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    url = f"sqlite:///{root / 'catalogue.sqlite'}"
    command.upgrade(_build_alembic_config(url), revision)
    engine = fk_enforced_engine(url)
    with engine.begin() as db:
        owner = scope(db)
        kid = key(db, owner, state="ACTIVE")
        control_update(db, mode="CUTOVER")
        control_update(db, mode="SECURE")
        gid = generation(db, approved_uid=os.geteuid())
        put(db, "credential_generation_keys", generation_id=gid, key_id=kid,
            key_epoch=1, domain="room", owner_id=owner)
    from uuid import UUID
    ref = KeyRef("room", UUID(owner), UUID(kid), 1)
    credentials = root / "credentials"
    credentials.mkdir(mode=0o700)
    entry = credentials / SystemdKeyVault.credential_name(ref)
    entry.write_text(json.dumps(["ambient-legacy/key", 1, ref.domain, owner, kid, 1,
                                os.urandom(32).hex()], separators=(",", ":")))
    entry.chmod(0o600)
    for name in ("operation.lock", "worker.lock"):
        (root / name).touch(mode=0o600)
    (root / "ciphertext").mkdir(mode=0o700)
    env = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"), HOME=str(root),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE="1",
        SECURITY_MODE="SECURE", DATABASE_URL=url, USE_CLOUD_SQL_CONNECTOR="false",
        USE_GCS_MEDIA_STORAGE="false", PREFER_OLLAMA_FOR_GEMMA="false",
        JWT_SECRET_KEY="synthetic-startup-signing-key-0123456789-ABCDEF",
        SECURE_UNIT_NAME="synthetic.service", SECURE_APPROVED_UID=str(os.geteuid()),
        SECURE_GENERATION_ID=gid, SECURE_CREDENTIALS_DIRECTORY=str(credentials),
        SECURE_CIPHERTEXT_DIRECTORY=str(root / "ciphertext"),
        SECURE_LOCK_PATH=str(root / "operation.lock"),
        SECURE_WORKER_LOCK_PATH=str(root / "worker.lock"), INVOCATION_ID=str(uuid4()))
    return engine, env


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() == 0, reason="native non-root Linux custody required")
@pytest.mark.parametrize("revision", ["0005_secure_catalogue", "0009_ocr_review", "0012_secure_room_storage_quota"])
def test_normal_application_lifespan_verifies_real_credential_generation(tmp_path, revision):
    engine, env = provision_startup(tmp_path, revision)
    try:
        result = subprocess.run([sys.executable, "-c", """
from fastapi.testclient import TestClient
from app.main import app
from app.services.secure_runtime import LinuxLock
from app.storage.key_vault import SystemdKeyVault
with TestClient(app):
    runtime = app.state.secure_runtime
    assert isinstance(runtime.lock, LinuxLock)
    assert isinstance(runtime.vault, SystemdKeyVault)
    assert runtime.worker_lock.fd is not None
    runtime.check_admission()
assert runtime.worker_lock.fd is None
print("normal-startup-pass")
"""], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "normal-startup-pass"
        with engine.connect() as db:
            assert db.scalar(text("SELECT state FROM credential_generations")) == "VERIFIED"
            assert db.scalar(text("SELECT admission FROM security_control")) == "OPEN"
    finally:
        engine.dispose()


@pytest.mark.parametrize("problem", ["unknown", "extra-head", "missing-review", "missing-trigger",
    "populated-graph", "fk-off", "legacy-album"])
def test_0009_schema_admission_rejects_drift_and_plaintext_extensions(tmp_path, problem):
    engine, _ = provision_startup(tmp_path)
    try:
        with engine.begin() as db:
            if problem == "unknown":
                db.exec_driver_sql("UPDATE alembic_version SET version_num='unsupported'")
            elif problem == "extra-head":
                db.exec_driver_sql("INSERT INTO alembic_version VALUES ('0005_secure_catalogue')")
            elif problem == "missing-review":
                db.exec_driver_sql("ALTER TABLE processing_jobs DROP COLUMN review_json")
            elif problem == "missing-trigger":
                name = db.scalar(text("SELECT name FROM sqlite_master WHERE type='trigger' LIMIT 1"))
                db.exec_driver_sql(f'DROP TRIGGER "{name}"')
            elif problem == "populated-graph":
                uid, rid = str(uuid4()), str(uuid4())
                put(db, "users", id=uid)
                put(db, "family_rooms", id=rid, owner_user_id=uid)
                put(db, "family_people", id=str(uuid4()), room_id=rid, display_name="synthetic")
            elif problem == "legacy-album":
                db.exec_driver_sql("CREATE TABLE albums (id TEXT PRIMARY KEY)")
        if problem == "fk-off":
            engine.dispose()
            engine = create_engine(engine.url)
        with pytest.raises(SecureUnavailable):
            validate_catalogue_mode(engine, "SECURE")
        with engine.connect() as db:
            assert db.scalar(text("SELECT state FROM credential_generations")) == "REQUESTED"
            assert db.scalar(text("SELECT admission FROM security_control")) == "CLOSED"
    finally:
        engine.dispose()


@pytest.mark.parametrize("revision", ["0006_family_graph", "0008_upload_file_uploader", "0010_merge_album_family"])
def test_other_known_schema_revisions_do_not_gain_secure_admission(tmp_path, revision):
    url = f"sqlite:///{tmp_path / 'unsupported.sqlite'}"
    command.upgrade(_build_alembic_config(url), revision)
    engine = fk_enforced_engine(url)
    try:
        # The album lineage is intrinsically closed LEGACY; do not bypass its fence.
        if revision != "0010_merge_album_family":
            with engine.begin() as db:
                control_update(db, mode="CUTOVER")
                control_update(db, mode="SECURE")
        with pytest.raises(SecureUnavailable):
            validate_catalogue_mode(engine, "SECURE")
    finally:
        engine.dispose()
