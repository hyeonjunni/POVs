"""Encrypted M3 fixture context within the inventory-managed room payload.

Inner envelopes are components, never independent objects or vault keys. Their
wrapping material lives ONLY inside the encrypted outer room object, so ordinary
room key rotation/inventory/deletion covers the entire container. Source gates
run before opening each component. No plaintext graph is persisted or spooled.
"""
import base64
import json
import secrets
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import secure_operations as ops
from app.api.routes.secure_media import abandon_staged
from app.core.database import Base
from app.models.family import FamilyPerson
from app.models.family_context import (
    AliasTarget,
    ContextEntity,
    ContextSource,
    Event,
    EventParticipation,
    ExternalDatasetRef,
    FamilyRelation,
    RelationAlias,
)
from app.services import family_context as plain
from app.services import secure_catalogue as cat
from app.services.dataset_bridge import _digest, import_fixture, load_package
from app.services.record_policy import allowed_record_ids
from app.storage.contracts import PayloadIdentity
from app.storage.crypto import open_payload, seal_payload

MODELS = (FamilyPerson, ContextEntity, ContextSource, FamilyRelation, RelationAlias,
          AliasTarget, Event, EventParticipation, ExternalDatasetRef)
TABLES = {model.__tablename__: model for model in MODELS}


def _state(db, uid, room_id) -> dict[str, Any]:
    """Opaque live policy snapshot; no source payloads or profile fields."""
    actor = db.execute(text("SELECT m.id,m.role FROM family_members m JOIN family_rooms r ON r.id=m.room_id "
        "WHERE m.room_id=:r AND m.user_id=:u AND m.is_active=true AND r.is_active=true "
        "AND NOT EXISTS (SELECT 1 FROM security_tombstones t WHERE "
        "(t.domain='room' AND t.owner_id=:r AND t.target_kind='room') OR (t.domain='user' AND t.owner_id=:u))"),
        {"r": room_id, "u": uid}).mappings().one_or_none()
    if actor is None:
        raise plain.unavailable()
    queries = {
        "family_rooms": "SELECT id,owner_user_id,is_active FROM family_rooms WHERE id=:r",
        "family_members": "SELECT id,room_id,user_id,role,is_active FROM family_members WHERE room_id=:r ORDER BY id",
        "uploads": "SELECT id,room_id,uploader_user_id,type FROM uploads WHERE room_id=:r ORDER BY id",
        "records": "SELECT id,room_id,owner_member_id,uploader_member_id,visibility,lifecycle_state,review_status,record_version,source_revision,authorization_epoch FROM records WHERE room_id=:r ORDER BY id",
        "record_grants": "SELECT * FROM record_grants WHERE record_id IN (SELECT id FROM records WHERE room_id=:r) ORDER BY record_id,member_id",
        "record_people": "SELECT * FROM record_people WHERE record_id IN (SELECT id FROM records WHERE room_id=:r) ORDER BY record_id,member_id,role",
        "record_restrictions": "SELECT id,record_id,member_id,active FROM record_restrictions WHERE record_id IN (SELECT id FROM records WHERE room_id=:r) ORDER BY id",
        "record_restriction_grants": "SELECT * FROM record_restriction_grants WHERE restriction_id IN (SELECT id FROM record_restrictions WHERE record_id IN (SELECT id FROM records WHERE room_id=:r)) ORDER BY restriction_id,member_id",
    }
    rows = {table: [dict(r) for r in db.execute(text(query), {"r": room_id}).mappings()] for table, query in queries.items()}
    allowed = sorted(allowed_record_ids(db, uid, room_id, purpose="rag"))
    return {"actor": dict(actor), "rows": rows, "allowed": allowed}


@contextmanager
def projection(state):
    """Process-memory-only reuse of the canonical deterministic M3 engine."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            users = {m["user_id"] for m in state["rows"]["family_members"]}
            users.update(r["owner_user_id"] for r in state["rows"]["family_rooms"])
            users.update(r["uploader_user_id"] for r in state["rows"]["uploads"] if r["uploader_user_id"])
            for uid in users:
                db.execute(Base.metadata.tables["users"].insert().values(id=uid))
            for table, rows in state["rows"].items():
                for row in rows:
                    # Core defaults apply, but no private metadata is copied.
                    db.execute(Base.metadata.tables[table].insert().values(**row))
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


def _row(model):
    return {c.name: (getattr(model, c.name).isoformat() if isinstance(getattr(model, c.name), date) else getattr(model, c.name)) for c in model.__table__.columns
            if c.name not in ("created_at", "updated_at")}


def _insert(db, rows):
    for table, model in TABLES.items():
        for row in rows.get(table, []):
            value = dict(row)
            if table == "family_people":
                for field in ("birth_date", "death_date"):
                    if isinstance(value.get(field), str):
                        value[field] = date.fromisoformat(value[field])
                if db.get(FamilyPerson, value["id"]) is not None:
                    continue
            db.add(model(**value))
            db.flush()


def _identity(room_id, entity_id, length):
    return PayloadIdentity("room", UUID(room_id), None, UUID(entity_id), 1,
                           "room_metadata", None, None, length)


def seal_component(room_id, entity_id, rows):
    raw = json.dumps(rows, default=str, sort_keys=True, separators=(",", ":")).encode()
    key = secrets.token_bytes(32)
    sealed = seal_payload(_identity(room_id, entity_id, len(raw)), raw, key)
    return {"id": entity_id, "length": len(raw), "key": base64.b64encode(key).decode(),
            "blob": base64.b64encode(sealed.blob).decode(), "wrapped": base64.b64encode(sealed.wrapped_dek).decode()}


def open_component(room_id, component):
    """Called only after live ACL/version gates, never for a denied component."""
    raw = open_payload(_identity(room_id, component["id"], component["length"]),
        base64.b64decode(component["blob"], validate=True), base64.b64decode(component["wrapped"], validate=True),
        base64.b64decode(component["key"], validate=True))
    return json.loads(raw)


def eligible(component, state):
    records = {r["id"]: r for r in state["rows"]["records"]}
    return all(s["record_id"] in state["allowed"] and
        records[s["record_id"]]["record_version"] == s["record_version"] and
        records[s["record_id"]]["source_revision"] == s["source_revision"] for s in component["sources"])


def _load(db, container, state, room_id):
    rows = {table: [] for table in TABLES}
    for package in container.get("packages", []):
        for table, values in package["public"].items():
            rows[table].extend(values)
        selected = {c["id"]: c for c in package["components"] if eligible(c, state)}
        while True:
            retained = {k: c for k, c in selected.items() if set(c["dependencies"]) <= selected.keys()}
            if len(retained) == len(selected):
                break
            selected = retained
        for component in selected.values():
            for table, values in open_component(room_id, component).items():
                rows[table].extend(values)
    # A participation or child event cannot reveal an unavailable parent.
    pending = rows["context_events"]
    ordered, ids = [], set()
    while pending:
        ready = [v for v in pending if v["parent_event_id"] is None or v["parent_event_id"] in ids]
        if not ready:
            break
        ordered.extend(ready)
        ids.update(v["id"] for v in ready)
        pending = [v for v in pending if v["id"] not in ids]
    rejected = {v["id"] for v in pending}
    rejected.update(v["id"] for v in rows["event_participation"] if v["event_id"] not in ids)
    rows["context_events"] = ordered
    rows["event_participation"] = [v for v in rows["event_participation"] if v["id"] not in rejected]
    rows["context_entities"] = [v for v in rows["context_entities"] if v["id"] not in rejected]
    rows["context_sources"] = [v for v in rows["context_sources"] if v["entity_id"] not in rejected]
    rows["external_dataset_refs"] = [v for v in rows["external_dataset_refs"] if v["runtime_id"] not in rejected]
    _insert(db, rows)


def _read(runtime, uid, room_id):
    with ops.tx() as db:
        if db.scalar(text("SELECT version_num FROM alembic_version")) != "0016_family_context":
            raise cat.SecureStateChanged()
    room, saved, _ = ops.read_room(runtime, uid, room_id)
    with ops.tx() as db:
        cat.revalidate(db, saved)
        state = _state(db, uid, room_id)
    container = room.get("m3_context", {"version": 1, "packages": []})
    if container.get("version") != 1:
        raise cat.SecureStateChanged()
    return room, saved, state, container


def _revalidate(db, saved, state, uid, room_id):
    cat.revalidate(db, saved)
    if _state(db, uid, room_id) != state:
        raise cat.SecureStateChanged()


def query(runtime, uid, room_id, operation, *args, **kwargs):
    """Same DTOs as LEGACY, under secure admission and final snapshot fences."""
    _, saved, state, container = _read(runtime, uid, room_id)
    with projection(state) as db:
        _load(db, container, state, room_id)
        if operation == "resolve_external":
            from app.services.dataset_bridge import resolve_external
            result = resolve_external(db, user_id=uid, room_id=room_id, **kwargs)
        else:
            result = getattr(plain, operation)(db, uid, room_id, *args, **kwargs)
    if operation == "structured_ask_query" and result is not None:
        # Denied components stay unopened and cannot influence this signal.
        # A visible but superseded binding must not promote another event.
        stale = any(c["kind"] in ("EVENT", "PARTICIPATION") and
            all(source["record_id"] in state["allowed"] for source in c["sources"]) and
            not eligible(c, state)
            for package in container.get("packages", []) for c in package["components"])
        if stale:
            from app.ai.grounded_answering import GroundedEvidenceChanged
            raise GroundedEvidenceChanged("STALE_SOURCE")
    with ops.tx() as db:
        _revalidate(db, saved, state, uid, room_id)
    return result


def import_encrypted_fixture(runtime, uid, room_id, directory: Path, *, member_bindings, record_bindings):
    """Internal synthetic-fixture bridge. Caller holds the secure admission guard.

    One atomic outer-room publication; no source/claim/index version mutation.
    Existing entries are immutable: a changed package requires explicit revision.
    """
    room, before, state, container = _read(runtime, uid, room_id)
    if state["actor"]["role"].lower() not in ("owner", "admin"):
        raise plain.unavailable()
    package = load_package(directory)
    family = package["family.json"][0]
    identity = [family["package_id"], family["family_ref"]]
    digest = _digest([package, member_bindings, record_bindings])
    if not set(record_bindings.values()) <= set(state["allowed"]):
        raise plain.unavailable()
    existing = next((p for p in container["packages"] if p["scope"] == identity), None)
    if existing:
        if existing["digest"] != digest or any(not eligible(c, state) for c in existing["components"]):
            raise ValueError("explicit dataset revision required")
        with ops.tx() as db:
            _revalidate(db, before, state, uid, room_id)
        return existing["mapping"]
    with projection(state) as db:
        _load(db, container, state, room_id)
        mapping = import_fixture(db, directory, room_id=room_id, actor_user_id=uid,
            member_bindings=member_bindings, record_bindings=record_bindings)
        refs = list(db.scalars(select(ExternalDatasetRef).where(ExternalDatasetRef.package_id == identity[0], ExternalDatasetRef.family_ref == identity[1])))
        people = list(db.scalars(select(FamilyPerson).where(FamilyPerson.id.in_(mapping["PERSON"].values()))))
        public = {"family_people": [_row(p) for p in people], "external_dataset_refs": []}
        components = []
        for ref in refs:
            if ref.kind in ("FAMILY", "PERSON"):
                public["external_dataset_refs"].append(_row(ref))
                continue
            rows = {"external_dataset_refs": [_row(ref)]}
            if ref.kind == "RECORD":
                row = next(r for r in state["rows"]["records"] if r["id"] == ref.runtime_id)
                sources = [{k: row[k if k != "record_id" else "id"] for k in ("record_id", "record_version", "source_revision")}]
                component_id = ref.id
            else:
                entity = db.get(ContextEntity, ref.runtime_id)
                rows["context_entities"] = [_row(entity)]
                sources = [_row(s) for s in db.scalars(select(ContextSource).where(ContextSource.entity_id == ref.runtime_id))]
                rows["context_sources"] = sources
                model = {"RELATION": FamilyRelation, "ALIAS": RelationAlias, "EVENT": Event, "PARTICIPATION": EventParticipation}[ref.kind]
                rows[model.__tablename__] = [_row(db.get(model, ref.runtime_id))]
                if ref.kind == "ALIAS":
                    rows["relation_alias_targets"] = [_row(t) for t in db.scalars(select(AliasTarget).where(AliasTarget.alias_id == ref.runtime_id))]
                component_id = ref.runtime_id
            dependencies = []
            if ref.kind == "EVENT":
                parent = rows["context_events"][0]["parent_event_id"]
                dependencies = [parent] if parent else []
            elif ref.kind == "PARTICIPATION":
                dependencies = [rows["event_participation"][0]["event_id"]]
            components.append(seal_component(room_id, component_id, rows) |
                {"sources": sources, "kind": ref.kind, "dependencies": dependencies})
    # Dependencies load before their children; never infer a hidden parent.
    order = {"RECORD": 0, "RELATION": 1, "ALIAS": 2, "EVENT": 3, "PARTICIPATION": 4}
    components.sort(key=lambda c: order[c["kind"]])
    new = {"scope": identity, "digest": digest, "mapping": mapping, "public": public, "components": components}
    updated = dict(room, m3_context={"version": 1, "packages": [*container["packages"], new]})
    # Validate serialization and the outer storage size before claiming an operation.
    from uuid import uuid4
    _identity(room_id, str(uuid4()), len(ops.raw_json(updated)))
    with ops.tx() as db:
        _revalidate(db, before, state, uid, room_id)
    staged = ops.stage_update(runtime, uid, ("room", UUID(room_id)), updated)
    try:
        if replace(staged[0], operation_id=None) != replace(before, operation_id=None):
            raise cat.SecureStateChanged()
        with ops.tx() as db:
            cat.lock_rooms(db, (room_id,))
            _revalidate(db, staged[0], state, uid, room_id)
            ops.publish_update(db, staged)
    except BaseException:
        abandon_staged([staged])
        raise
    return mapping
