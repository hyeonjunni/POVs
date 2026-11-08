from pathlib import Path

import pytest
from alembic import command
from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config, verify_revision
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_migrations import _seed_retrieval_parents


def test_backfill_preserves_uploads_and_security_fences(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm2.sqlite'}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0014_native_text")
    engine = fk_enforced_engine(url)
    _seed_retrieval_parents(engine)
    with engine.begin() as db:
        db.execute(text("INSERT INTO family_members(id,room_id,user_id) VALUES ('member','r-1','u-1')"))
        before = db.execute(text("SELECT * FROM uploads ORDER BY id")).all()
        triggers = db.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")).all()
    command.upgrade(config, "0015_record_privacy")
    with engine.connect() as db:
        assert verify_revision(db, "0015_record_privacy") == []
        assert db.execute(text("SELECT * FROM uploads ORDER BY id")).all() == before
        assert db.execute(text("SELECT id,owner_member_id,visibility,review_status FROM records ORDER BY id")).all() == [
            ("rec-1", "member", "FAMILY", "APPROVED"), ("rec-2", None, "FAMILY", "APPROVED")]
        after = db.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")).all()
        assert all(item in after for item in triggers)
    with pytest.raises(RuntimeError, match="policy or history"):
        command.downgrade(config, "0014_native_text")
    engine.dispose()


@pytest.mark.parametrize("mutation", [
    "UPDATE records SET metadata_json='{}' WHERE id='rec-1'",
    "INSERT INTO record_metadata_revisions(id,record_id,actor_member_id,metadata_revision,changed_fields,reason_digest,source_affecting,legacy_detail) VALUES ('revision','rec-1','member',1,'[]','digest',false,'{}')",
])
def test_secure_mode_rejects_new_plaintext_metadata(tmp_path: Path, mutation: str) -> None:
    url = f"sqlite:///{tmp_path / 'secure.sqlite'}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0014_native_text")
    engine = fk_enforced_engine(url)
    _seed_retrieval_parents(engine)
    with engine.begin() as db:
        db.execute(text("INSERT INTO family_members(id,room_id,user_id) VALUES ('member','r-1','u-1')"))
    command.upgrade(config, "0015_record_privacy")
    with engine.begin() as db:
        db.execute(text("UPDATE security_control SET mode='CUTOVER',revision=revision+1 WHERE id=1"))
    with pytest.raises(IntegrityError, match="rejects plaintext writer"), engine.begin() as db:
        db.execute(text(mutation))
    engine.dispose()


def test_audit_is_append_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'audit.sqlite'}"
    command.upgrade(_build_alembic_config(url), "0015_record_privacy")
    engine = fk_enforced_engine(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO record_audits(id,record_id,actor_user_id,action,before_version,after_version) VALUES ('audit','missing','actor','ACCESS_DENIED',0,0)"))
    with pytest.raises(IntegrityError, match="immutable record audit"), engine.begin() as db:
        db.execute(text("DELETE FROM record_audits"))
    engine.dispose()
