"""Disposable SQLite evidence for joining both deployed revision lineages."""
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from conftest import fk_enforced_engine
from scripts.adopt_alembic import (
    MERGED_HEAD, REVISION_PARENTS, _build_alembic_config, adopt,
    recorded_revisions, verify_frontier, verify_revision,
)
from test_migrations import _seed_retrieval_parents, schema_fingerprint

TEAM = "0007_video_albums_reads"
FEATURE = "0009_ocr_review"


def test_deployable_migration_dag_has_one_merge_head():
    scripts = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    assert scripts.get_heads() == [MERGED_HEAD]
    for revision, parents in REVISION_PARENTS.items():
        actual = scripts.get_revision(revision).down_revision
        assert (() if actual is None else (actual,) if isinstance(actual, str) else actual) == parents


def snapshot(engine):
    with engine.connect() as db:
        return {
            table: (
                [c["name"] for c in inspect(db).get_columns(table)],
                db.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1').all(),
            )
            for table in inspect(db).get_table_names()
        }


def assert_preserved(engine, before):
    with engine.connect() as db:
        for table, (columns, rows) in before.items():
            if table != "alembic_version":
                names = ",".join(columns)
                assert db.exec_driver_sql(f'SELECT {names} FROM "{table}" ORDER BY 1').all() == rows
        assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def seed_branch(engine, branch):
    with engine.begin() as db:
        if branch == FEATURE:
            db.exec_driver_sql("INSERT INTO family_people(id,room_id,display_name) VALUES ('p1','r-1','Parent'),('p2','r-1','Child')")
            db.exec_driver_sql("INSERT INTO family_relationships(id,room_id,from_person_id,to_person_id,relation_type) VALUES ('edge','r-1','p1','p2','PARENT_OF')")
            db.exec_driver_sql("UPDATE upload_files SET file_uploader_user_id='u-1' WHERE id='f-1'")
            db.exec_driver_sql("UPDATE processing_jobs SET review_json='{}' WHERE id='job-1'")
        else:
            db.exec_driver_sql("INSERT INTO albums(id,room_id,title,tags,sort_order,version,media_kind) VALUES ('photo','r-1','Photo','[]',0,1,'image'),('video','r-1','Video','[]',1,1,'video')")
            db.exec_driver_sql("INSERT INTO album_photos(album_id,upload_id,room_id,sort_order) VALUES ('photo','rec-1','r-1',0)")
            db.exec_driver_sql("INSERT INTO album_videos(album_id,upload_id,room_id,sort_order) VALUES ('video','rec-1','r-1',0)")
            db.exec_driver_sql("INSERT INTO record_read_receipts(user_id,upload_id,room_id) VALUES ('u-1','rec-1','r-1')")


@pytest.mark.parametrize("first,second", [(TEAM, FEATURE), (FEATURE, TEAM)])
def test_populated_branches_converge_in_both_orders_and_repeat(tmp_path, first, second):
    path = tmp_path / "populated.sqlite"
    url = f"sqlite:///{path}"
    config = _build_alembic_config(url)
    command.upgrade(config, first)
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        seed_branch(engine, first)
        before = snapshot(engine)
        command.upgrade(config, second)
        assert_preserved(engine, before)
        with engine.connect() as db:
            assert recorded_revisions(db) == sorted((TEAM, FEATURE))
            assert verify_frontier(db, (TEAM, FEATURE)) == []
        seed_branch(engine, second)
        before = snapshot(engine)
        assert adopt(url, check_only=True) == ",".join(sorted((TEAM, FEATURE)))
        assert adopt(url) == ",".join(sorted((TEAM, FEATURE)))
        assert_preserved(engine, before)
        with engine.connect() as db:
            assert recorded_revisions(db) == [MERGED_HEAD]
            assert verify_revision(db, MERGED_HEAD) == []
        before_bytes = path.read_bytes()
        assert adopt(url) == MERGED_HEAD
        command.upgrade(config, "head")
        assert path.read_bytes() == before_bytes
    finally:
        engine.dispose()


@pytest.mark.parametrize("start", [
    None, "0005_secure_catalogue", "0006_photo_albums", TEAM,
    "0006_family_graph", "0008_upload_file_uploader", FEATURE,
])
def test_every_fresh_or_deployed_intermediate_converges(tmp_path, start):
    url = f"sqlite:///{tmp_path / 'path.sqlite'}"
    if start:
        command.upgrade(_build_alembic_config(url), start)
    assert adopt(url) == (start or "fresh")
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as db:
            assert recorded_revisions(db) == [MERGED_HEAD]
            assert verify_revision(db, MERGED_HEAD) == []
        assert adopt(url, check_only=True) == MERGED_HEAD
    finally:
        engine.dispose()


@pytest.mark.parametrize("category,sql,reason", [
    ("graph", "INSERT INTO family_people(id,room_id,display_name) VALUES ('p','r-1','Person')", "family graph data"),
    ("uploader", "UPDATE upload_files SET file_uploader_user_id='u-1' WHERE id='f-1'", "uploader attribution"),
    ("review", "UPDATE processing_jobs SET review_json='{}' WHERE id='job-1'", "OCR review metadata"),
    ("photo", "INSERT INTO albums(id,room_id,title,tags,sort_order,version) VALUES ('a','r-1','A','[]',0,1)", "album tables"),
    ("video", "INSERT INTO albums(id,room_id,title,tags,sort_order,version,media_kind) VALUES ('a','r-1','A','[]',0,1,'video')", "video albums"),
    ("receipt", "INSERT INTO record_read_receipts(user_id,upload_id,room_id) VALUES ('u-1','rec-1','r-1')", "read receipts"),
])
def test_merged_downgrade_guards_roll_back_entire_dag(tmp_path, category, sql, reason):
    path = tmp_path / f"{category}.sqlite"
    url = f"sqlite:///{path}"
    adopt(url)
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        with engine.begin() as db:
            db.exec_driver_sql(sql)
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match=reason):
            command.downgrade(_build_alembic_config(url), "0005_secure_catalogue")
        assert path.read_bytes() == before
        with engine.connect() as db:
            assert recorded_revisions(db) == [MERGED_HEAD]
            assert verify_revision(db, MERGED_HEAD) == []
    finally:
        engine.dispose()


def test_empty_merge_downgrade_frontier_and_full_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path / 'roundtrip.sqlite'}"
    config = _build_alembic_config(url)
    adopt(url)
    engine = fk_enforced_engine(url)
    try:
        before = schema_fingerprint(engine)
        command.downgrade(config, TEAM)
        with engine.connect() as db:
            assert recorded_revisions(db) == sorted((TEAM, FEATURE))
            assert verify_frontier(db, (TEAM, FEATURE)) == []
        adopt(url)
        assert schema_fingerprint(engine) == before
        command.downgrade(config, "0005_secure_catalogue")
        with engine.connect() as db:
            assert verify_revision(db, "0005_secure_catalogue") == []
        adopt(url)
        assert schema_fingerprint(engine) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["CUTOVER", "SECURE", "QUARANTINED"])
@pytest.mark.parametrize("start", ["0005_secure_catalogue", FEATURE])
def test_nonlegacy_default_preserves_lineage_and_only_explicit_secure_bridge_advances(tmp_path, mode, start):
    path = tmp_path / "nonlegacy.sqlite"
    url = f"sqlite:///{path}"
    config = _build_alembic_config(url)
    command.upgrade(config, start)
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as db:
            if mode == "SECURE":
                db.exec_driver_sql("UPDATE security_control SET mode='CUTOVER',revision=revision+1")
            db.exec_driver_sql(f"UPDATE security_control SET mode='{mode}',revision=revision+1")
        before = path.read_bytes()
        assert adopt(url) == start
        assert adopt(url, check_only=True) == start
        for target in ("0006_photo_albums", TEAM, "0011_legacy_room_storage_quota"):
            with pytest.raises(RuntimeError, match="non-LEGACY"):
                adopt(url, target_revision=target)
            assert path.read_bytes() == before
        with pytest.raises(RuntimeError, match="closed LEGACY"):
            command.upgrade(config, "head")
        assert path.read_bytes() == before
        if start == FEATURE and mode == "SECURE":
            assert adopt(url, target_revision=MERGED_HEAD) == start
            with engine.connect() as db:
                assert recorded_revisions(db) == [MERGED_HEAD]
                assert verify_revision(db, MERGED_HEAD) == []
        else:
            with pytest.raises(RuntimeError):
                adopt(url, target_revision=MERGED_HEAD)
            assert path.read_bytes() == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("damage,reason", [
    ("unknown", "Unrecognized revision"),
    ("redundant", "redundant ancestors"),
    ("wrong_stamp", "unexpected"),
    ("missing_graph", "family_people"),
    ("missing_review", "review_json"),
    ("missing_fence", "triggers differ"),
])
def test_drift_and_ambiguous_frontier_refused_without_mutation(tmp_path, damage, reason):
    path = tmp_path / "drift.sqlite"
    url = f"sqlite:///{path}"
    adopt(url)
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as db:
            if damage == "unknown":
                db.exec_driver_sql("UPDATE alembic_version SET version_num='unknown'")
            elif damage == "redundant":
                db.exec_driver_sql("INSERT INTO alembic_version VALUES ('0005_secure_catalogue')")
            elif damage == "wrong_stamp":
                db.exec_driver_sql(f"UPDATE alembic_version SET version_num='{TEAM}'")
            elif damage == "missing_graph":
                db.exec_driver_sql("DROP TABLE family_people")
            elif damage == "missing_review":
                db.exec_driver_sql("ALTER TABLE processing_jobs DROP COLUMN review_json")
            else:
                db.exec_driver_sql("DROP TRIGGER album_legacy_cutover_fence")
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match=reason):
            adopt(url)
        assert path.read_bytes() == before
        with pytest.raises(RuntimeError, match=reason):
            command.upgrade(_build_alembic_config(url), "head")
        assert path.read_bytes() == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("start,target", [
    (FEATURE, TEAM),
    (TEAM, FEATURE),
    (MERGED_HEAD, FEATURE),
    (MERGED_HEAD, "0005_secure_catalogue"),
])
def test_adoption_never_discards_an_applied_branch(tmp_path, start, target):
    path = tmp_path / "target.sqlite"
    url = f"sqlite:///{path}"
    command.upgrade(_build_alembic_config(url), start)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="cannot downgrade or discard"):
        adopt(url, target_revision=target)
    assert path.read_bytes() == before


def test_intermediate_dual_frontier_matches_fresh_merged_schema(tmp_path):
    path = tmp_path / "dual.sqlite"
    url = f"sqlite:///{path}"
    config = _build_alembic_config(url)
    command.upgrade(config, "0006_family_graph")
    command.upgrade(config, "0006_photo_albums")
    assert adopt(url, check_only=True) == "0006_family_graph,0006_photo_albums"
    adopt(url)
    reference_url = f"sqlite:///{tmp_path / 'fresh.sqlite'}"
    adopt(reference_url)
    engine = fk_enforced_engine(url)
    reference = fk_enforced_engine(reference_url)
    try:
        assert schema_fingerprint(engine) == schema_fingerprint(reference)
    finally:
        engine.dispose()
        reference.dispose()
