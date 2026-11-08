"""Frozen additive M3 schema. No changes to legacy family graph or records."""
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ContextEntity(Base):
    __tablename__ = "context_entities"
    __table_args__ = (
        UniqueConstraint("room_id", "id", name="uq_context_room_id"),
        CheckConstraint("kind IN ('RELATION','ALIAS','EVENT','PARTICIPATION')", name="ck_context_kind"),
        CheckConstraint("review_status IN ('APPROVED','NEEDS_REVIEW')", name="ck_context_review"),
        CheckConstraint("revision >= 1", name="ck_context_revision"),
    )
    id = Column(String, primary_key=True)
    room_id = Column(String, ForeignKey("family_rooms.id"), nullable=False)
    kind = Column(String, nullable=False)
    review_status = Column(String, nullable=False, server_default="APPROVED")
    revision = Column(Integer, nullable=False, server_default="1")


class ContextSource(Base):
    __tablename__ = "context_sources"
    entity_id = Column(String, ForeignKey("context_entities.id", ondelete="CASCADE"), primary_key=True)
    record_id = Column(String, ForeignKey("records.id"), primary_key=True)
    record_version = Column(Integer, nullable=False)
    source_revision = Column(Integer, nullable=False)


def entity_fk():
    return ForeignKeyConstraint(["room_id", "id"], ["context_entities.room_id", "context_entities.id"], ondelete="CASCADE")


def person_fk(column):
    return ForeignKeyConstraint(["room_id", column], ["family_people.room_id", "family_people.id"])


class FamilyRelation(Base):
    __tablename__ = "context_relations"
    __table_args__ = (entity_fk(), person_fk("from_person_id"), person_fk("to_person_id"),
        CheckConstraint("from_person_id != to_person_id", name="ck_context_relation_self"),
        CheckConstraint("relation_type IN ('PARENT_OF','SPOUSE_OF','SIBLING_OF','GUARDIAN_OF','OTHER')", name="ck_context_relation_type"),
        CheckConstraint("relation_type NOT IN ('SPOUSE_OF','SIBLING_OF') OR from_person_id < to_person_id", name="ck_context_relation_order"))
    id = Column(String, primary_key=True)
    room_id = Column(String, nullable=False)
    from_person_id = Column(String, nullable=False)
    to_person_id = Column(String, nullable=False)
    relation_type = Column(String, nullable=False)
    valid_from = Column(JSON, nullable=False)
    valid_to = Column(JSON, nullable=False)


class RelationAlias(Base):
    __tablename__ = "relation_aliases"
    __table_args__ = (entity_fk(), person_fk("speaker_person_id"), person_fk("viewer_person_id"),
        UniqueConstraint("room_id", "id", name="uq_alias_room_id"))
    id = Column(String, primary_key=True)
    room_id = Column(String, nullable=False)
    speaker_person_id = Column(String, nullable=False)
    viewer_person_id = Column(String, nullable=True)
    alias = Column(String, nullable=False)
    valid_from = Column(JSON, nullable=False)
    valid_to = Column(JSON, nullable=False)


class AliasTarget(Base):
    __tablename__ = "relation_alias_targets"
    __table_args__ = (ForeignKeyConstraint(["room_id", "alias_id"], ["relation_aliases.room_id", "relation_aliases.id"], ondelete="CASCADE"), person_fk("person_id"))
    alias_id = Column(String, primary_key=True)
    person_id = Column(String, primary_key=True)
    room_id = Column(String, nullable=False)


class Event(Base):
    __tablename__ = "context_events"
    __table_args__ = (entity_fk(), UniqueConstraint("room_id", "id", name="uq_context_event_room"),
        CheckConstraint("event_status IN ('PLANNED','ONGOING','COMPLETED','CANCELLED','UNKNOWN')", name="ck_context_event_status"),
        ForeignKeyConstraint(["room_id", "parent_event_id"], ["context_events.room_id", "context_events.id"]))
    id = Column(String, primary_key=True)
    room_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    category = Column(String, nullable=False)
    event_status = Column(String, nullable=False)
    occurred_at = Column(JSON, nullable=False)
    location = Column(JSON, nullable=False)
    parent_event_id = Column(String, nullable=True)
    phase_ref = Column(String, nullable=True)
    exam_id = Column(String, nullable=True)


class EventParticipation(Base):
    __tablename__ = "event_participation"
    __table_args__ = (entity_fk(), person_fk("person_id"),
        ForeignKeyConstraint(["room_id", "event_id"], ["context_events.room_id", "context_events.id"]),
        UniqueConstraint("event_id", "person_id", "phase_ref", "mode", name="uq_context_participation"),
        CheckConstraint("mode IN ('ONSITE','REMOTE','SUPPORT','MENTIONED','NOT_PRESENT')", name="ck_context_participation_mode"))
    id = Column(String, primary_key=True)
    room_id = Column(String, nullable=False)
    event_id = Column(String, nullable=False)
    person_id = Column(String, nullable=False)
    phase_ref = Column(String, nullable=False, server_default="")
    mode = Column(String, nullable=False)
    occurred_at = Column(JSON, nullable=False)
    activity = Column(String, nullable=True)


class ExternalDatasetRef(Base):
    __tablename__ = "external_dataset_refs"
    __table_args__ = (UniqueConstraint("room_id", "package_id", "family_ref", "kind", "external_dataset_id", name="uq_dataset_external_scope"),)
    id = Column(String, primary_key=True)
    room_id = Column(String, ForeignKey("family_rooms.id"), nullable=False)
    package_id = Column(String, nullable=False)
    family_ref = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    external_dataset_id = Column(String, nullable=False)
    runtime_id = Column(String, nullable=False)
    content_sha256 = Column(String, nullable=False)


revision = "0016_family_context"
down_revision = "0015_record_privacy"
branch_labels = depends_on = None
TABLES = ("context_entities", "context_sources", "context_relations", "relation_aliases", "relation_alias_targets", "context_events", "event_participation", "external_dataset_refs")


def schema_metadata():
    from sqlalchemy import Table
    metadata = Base.metadata
    if "family_rooms" not in metadata.tables:
        Table("family_rooms", metadata, Column("id", String, primary_key=True))
        Table("family_people", metadata, Column("id", String, primary_key=True), Column("room_id", String), UniqueConstraint("room_id", "id"))
        Table("records", metadata, Column("id", String, primary_key=True))
    return metadata


def upgrade():
    from alembic import op
    metadata = schema_metadata()
    metadata.create_all(op.get_bind(), tables=[t for t in metadata.sorted_tables if t.name in TABLES], checkfirst=False)
    if op.get_bind().dialect.name == "sqlite":
        for statement in trigger_statements():
            op.get_bind().exec_driver_sql(statement)


def downgrade():
    from alembic import op
    from sqlalchemy import func, select
    metadata = schema_metadata()
    connection = op.get_bind()
    if any(connection.scalar(select(func.count()).select_from(metadata.tables[name])) for name in TABLES):
        raise RuntimeError("0016 downgrade refused: M3 context or external identities would be lost")
    for table in reversed(metadata.sorted_tables):
        if table.name in TABLES:
            table.drop(connection)


def verify(connection):
    from scripts.adopt_alembic import _normalize_check, _normalize_default
    from sqlalchemy import inspect
    metadata = schema_metadata()
    inspector = inspect(connection)
    failures = []
    for name in TABLES:
        if not inspector.has_table(name):
            failures.append(f"M3 table {name} missing")
            continue
        table = metadata.tables[name]
        actual = {c["name"]: (str(c["type"]).upper(), c["nullable"], _normalize_default(c["default"])) for c in inspector.get_columns(name)}
        expected = {c.name: (str(c.type.compile(dialect=connection.dialect)).upper(), c.nullable, _normalize_default(c.server_default.arg) if c.server_default else None) for c in table.columns}
        if actual != expected:
            failures.append(f"M3 columns differ: {name}")
        if tuple(inspector.get_pk_constraint(name)["constrained_columns"]) != tuple(c.name for c in table.primary_key):
            failures.append(f"M3 primary key differs: {name}")
        if {(x["name"], tuple(x["column_names"])) for x in inspector.get_unique_constraints(name)} != {(x.name, tuple(c.name for c in x.columns)) for x in table.constraints if isinstance(x, UniqueConstraint)}:
            failures.append(f"M3 uniqueness differs: {name}")
        if {(x["name"], _normalize_check(x["sqltext"])) for x in inspector.get_check_constraints(name)} != {(x.name, _normalize_check(x.sqltext)) for x in table.constraints if isinstance(x, CheckConstraint)}:
            failures.append(f"M3 checks differ: {name}")
        actual_fk = {(tuple(x["constrained_columns"]), x["referred_table"], tuple(x["referred_columns"]), x.get("options", {}).get("ondelete")) for x in inspector.get_foreign_keys(name)}
        expected_fk = {(tuple(x.column_keys), x.referred_table.name, tuple(e.column.name for e in x.elements), x.ondelete) for x in table.constraints if isinstance(x, ForeignKeyConstraint)}
        if actual_fk != expected_fk:
            failures.append(f"M3 foreign keys differ: {name}")
    if connection.dialect.name == "sqlite":
        actual = {row[0]: _normalize_check(row[1]) for row in connection.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'm3_context_%'")}
        expected = {sql.split()[2]: _normalize_check(sql) for sql in trigger_statements()}
        if actual != expected:
            failures.append("M3 plaintext writer fences differ")
    return failures


def trigger_statements():
    return [
        f"CREATE TRIGGER m3_context_{table}_{operation.lower()} BEFORE {operation} ON {table} "
        "WHEN EXISTS (SELECT 1 FROM security_control WHERE id=1 AND mode <> 'LEGACY') "
        "BEGIN SELECT RAISE(ABORT, 'M3 plaintext context unavailable in secure mode'); END"
        for table in TABLES for operation in ("INSERT", "UPDATE")
    ]
