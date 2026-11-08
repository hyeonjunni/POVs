from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config, adopt, verify_revision
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from test_migration_dag import assert_preserved, snapshot
from test_migrations import _chunk_values, _seed_retrieval_parents


@pytest.fixture
def populated_catalogue(tmp_path: Path) -> Iterator[tuple[str, Engine]]:
    url = f"sqlite:///{tmp_path / 'native-text.sqlite'}"
    command.upgrade(_build_alembic_config(url), "0013_secure_reconciliation")
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        with engine.begin() as db:
            db.exec_driver_sql("UPDATE processing_jobs SET processor='stt' WHERE id='job-2'")
            db.exec_driver_sql("UPDATE processing_jobs SET review_json='{}' WHERE id='job-1'")
            metadata = sa.MetaData()
            chunks = sa.Table("record_chunks", metadata, autoload_with=db)
            embeddings = sa.Table("record_embeddings", metadata, autoload_with=db)
            db.execute(chunks.insert().values(**_chunk_values()))
            db.execute(embeddings.insert().values(
                id="embedding-1", chunk_id="chunk-1", model_key="synthetic:test",
                dimensions=1024, embedding="[]",
            ))
        yield url, engine
    finally:
        engine.dispose()


def test_upgrade_preserves_populated_catalogue(populated_catalogue: tuple[str, Engine]) -> None:
    # Given
    url, engine = populated_catalogue
    before = snapshot(engine)
    with engine.connect() as db:
        assert verify_revision(db, "0013_secure_reconciliation") == []
        triggers = db.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).all()
    # When
    command.upgrade(_build_alembic_config(url), "0014_native_text")
    # Then
    assert_preserved(engine, before)
    with engine.connect() as db:
        assert verify_revision(db, "0014_native_text") == []
        assert db.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).all() == triggers
    assert adopt(url, check_only=True) == "0014_native_text"


def test_text_job_is_accepted_after_upgrade(populated_catalogue: tuple[str, Engine]) -> None:
    # Given
    url, engine = populated_catalogue
    command.upgrade(_build_alembic_config(url), "0014_native_text")
    # When
    with engine.begin() as db:
        db.exec_driver_sql(
            "INSERT INTO processing_jobs(id,record_id,upload_file_id,room_id,processor) "
            "VALUES ('text-job','rec-1','f-1','r-1','text')"
        )
    # Then
    with engine.connect() as db:
        assert db.exec_driver_sql("SELECT processor FROM processing_jobs WHERE id='text-job'").scalar_one() == "text"


@pytest.mark.parametrize("mutation", [
    "UPDATE processing_jobs SET processor='unknown' WHERE id='job-1'",
    "UPDATE processing_jobs SET upload_file_id='missing' WHERE id='job-1'",
    "UPDATE record_chunks SET processing_job_id='job-2' WHERE id='chunk-1'",
])
def test_upgraded_catalogue_rejects_invalid_jobs(
    populated_catalogue: tuple[str, Engine], mutation: str,
) -> None:
    # Given
    url, engine = populated_catalogue
    command.upgrade(_build_alembic_config(url), "0014_native_text")
    # When / Then
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.exec_driver_sql(mutation)


def test_plaintext_fence_survives_upgrade(populated_catalogue: tuple[str, Engine]) -> None:
    # Given
    url, engine = populated_catalogue
    command.upgrade(_build_alembic_config(url), "0014_native_text")
    with engine.begin() as db:
        db.exec_driver_sql("UPDATE security_control SET mode='CUTOVER',revision=revision+1 WHERE id=1")
    # When / Then
    with pytest.raises(IntegrityError, match="rejects plaintext writer"), engine.begin() as db:
        db.exec_driver_sql("UPDATE processing_jobs SET result_json='{}' WHERE id='job-1'")


def test_downgrade_refuses_native_jobs_atomically(populated_catalogue: tuple[str, Engine]) -> None:
    # Given
    url, engine = populated_catalogue
    config = _build_alembic_config(url)
    command.upgrade(config, "0014_native_text")
    with engine.begin() as db:
        db.exec_driver_sql("UPDATE processing_jobs SET processor='text' WHERE id='job-1'")
    database = engine.url.database
    assert database is not None
    path = Path(database)
    before = path.read_bytes()
    # When
    with pytest.raises(RuntimeError, match="native text jobs"):
        command.downgrade(config, "0013_secure_reconciliation")
    # Then
    assert path.read_bytes() == before


def test_downgrade_without_native_jobs_preserves_rows(populated_catalogue: tuple[str, Engine]) -> None:
    # Given
    url, engine = populated_catalogue
    config = _build_alembic_config(url)
    command.upgrade(config, "0014_native_text")
    before = snapshot(engine)
    # When
    command.downgrade(config, "0013_secure_reconciliation")
    # Then
    assert_preserved(engine, before)
    with engine.connect() as db:
        assert verify_revision(db, "0013_secure_reconciliation") == []
    with pytest.raises(IntegrityError, match="ck_processing_job_processor_allowed"), engine.begin() as db:
        db.exec_driver_sql("UPDATE processing_jobs SET processor='text' WHERE id='job-1'")


def test_check_only_adoption_rejects_wrong_processor_check(populated_catalogue: tuple[str, Engine]) -> None:
    # Given: the old schema is falsely stamped with the new revision.
    url, engine = populated_catalogue
    with engine.begin() as db:
        db.exec_driver_sql("UPDATE alembic_version SET version_num='0014_native_text'")
    database = engine.url.database
    assert database is not None
    path = Path(database)
    before = path.read_bytes()
    # When / Then
    with pytest.raises(RuntimeError, match="processing_jobs.*CHECK"):
        adopt(url, check_only=True)
    assert path.read_bytes() == before
