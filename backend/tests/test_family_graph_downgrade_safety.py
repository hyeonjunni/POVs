"""Disposable SQLite regression for the independent graph downgrade attack."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from conftest import fk_enforced_engine


@pytest.mark.parametrize("state", ["empty", "person", "relationship"])
def test_graph_downgrade_refuses_populated_state_before_any_drop(state):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0006_family_graph.py"
    spec = importlib.util.spec_from_file_location("graph_downgrade_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = fk_enforced_engine()
    try:
        with engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE users (id TEXT PRIMARY KEY)")
            db.exec_driver_sql("CREATE TABLE family_rooms (id TEXT PRIMARY KEY)")
            db.exec_driver_sql("INSERT INTO family_rooms VALUES ('synthetic-room')")
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
                if state != "empty":
                    db.exec_driver_sql(
                        "INSERT INTO family_people (id,room_id,display_name) "
                        "VALUES ('p1','synthetic-room','Synthetic Parent')"
                    )
                if state == "relationship":
                    db.exec_driver_sql(
                        "INSERT INTO family_people (id,room_id,display_name) "
                        "VALUES ('p2','synthetic-room','Synthetic Child')"
                    )
                    db.exec_driver_sql(
                        "INSERT INTO family_relationships "
                        "(id,room_id,from_person_id,to_person_id,relation_type) "
                        "VALUES ('r1','synthetic-room','p1','p2','PARENT_OF')"
                    )
                before = {
                    table: db.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").all()
                    for table in ("family_people", "family_relationships")
                }
                if state == "empty":
                    migration.downgrade()
                    assert not set(before) & set(inspect(db).get_table_names())
                else:
                    with pytest.raises(RuntimeError, match="refusing downgrade: family graph data would be lost"):
                        migration.downgrade()
                    assert set(before) <= set(inspect(db).get_table_names())
                    for table, rows in before.items():
                        assert db.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").all() == rows
    finally:
        engine.dispose()
