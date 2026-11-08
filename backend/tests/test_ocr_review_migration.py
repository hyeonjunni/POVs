import pytest
from alembic import command
from sqlalchemy import inspect
from scripts.adopt_alembic import _build_alembic_config, adopt, verify_revision
from conftest import fk_enforced_engine
from test_migrations import _seed_retrieval_parents, _chunk_values


def test_review_migration_preserves_exact_triggers_rows_and_refuses_data_loss(tmp_path):
    import sqlalchemy as sa
    path = tmp_path / "review.sqlite"
    url = f"sqlite:///{path}"
    config = _build_alembic_config(url)
    command.upgrade(config,"0008_upload_file_uploader")
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        with engine.begin() as db:
            chunks = sa.Table("record_chunks",sa.MetaData(),autoload_with=db)
            db.execute(chunks.insert().values(**_chunk_values()))
        with engine.connect() as db:
            columns = {t:[c["name"] for c in inspect(db).get_columns(t)]
                       for t in ("processing_jobs","record_chunks","record_embeddings")}
            rows = {t:db.exec_driver_sql(f"SELECT {','.join(cols)} FROM {t} ORDER BY id").all()
                    for t,cols in columns.items()}
            triggers = db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()
        command.upgrade(config,"0009_ocr_review")
        with engine.connect() as db:
            assert verify_revision(db,"0009_ocr_review") == []
            for t,cols in columns.items():
                assert db.exec_driver_sql(f"SELECT {','.join(cols)} FROM {t} ORDER BY id").all() == rows[t]
            assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all() == triggers
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        before = path.read_bytes()
        assert adopt(url, target_revision="0009_ocr_review") == "0009_ocr_review"
        assert adopt(url,check_only=True) == "0009_ocr_review"
        assert path.read_bytes() == before
        with engine.begin() as db:
            db.exec_driver_sql("UPDATE processing_jobs SET review_json='{}'")
        with pytest.raises(RuntimeError,match="metadata would be lost"):
            command.downgrade(config,"0008_upload_file_uploader")
        with engine.begin() as db:
            db.exec_driver_sql("UPDATE processing_jobs SET review_json=NULL")
        command.downgrade(config,"0008_upload_file_uploader")
        with engine.connect() as db:
            assert verify_revision(db,"0008_upload_file_uploader") == []
            assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all() == triggers
    finally:
        engine.dispose()


@pytest.mark.parametrize("drift", ["missing","type","nullable","default"])
def test_review_adoption_rejects_drift_without_mutation(tmp_path,drift):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from scripts.adopt_alembic import migration_engine, migration_transaction
    import sqlalchemy as sa
    path = tmp_path / "drift.sqlite"
    url = f"sqlite:///{path}"
    command.upgrade(_build_alembic_config(url),"head")
    engine = migration_engine(url)
    try:
        with engine.connect() as db, migration_transaction(db):
            with Operations(MigrationContext.configure(db)).batch_alter_table("processing_jobs") as batch:
                if drift == "missing": batch.drop_column("review_json")
                elif drift == "type": batch.alter_column("review_json",type_=sa.Integer())
                elif drift == "nullable": batch.alter_column("review_json",nullable=False)
                else: batch.alter_column("review_json",server_default="unknown")
    finally:
        engine.dispose()
    before = path.read_bytes()
    with pytest.raises(RuntimeError,match="review_json"):
        adopt(url)
    assert path.read_bytes() == before
