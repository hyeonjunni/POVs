"""Explicit synthetic fixture bridge; no public import endpoint or DOCX parser.

Existing account/record bindings are caller-supplied and authorized. Claims/media/
queries are validated but not materialized: those belong to later milestones.
"""
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.family import FamilyMember, FamilyPerson
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
from app.schemas.family_context import TemporalValue
from app.services.family_context import membership, unavailable
from app.services.record_policy import require_record_access

SCHEMA_PATH = Path(__file__).parents[1] / "contracts" / "dataset_v1.schema.json"


def load_package(directory: Path) -> dict[str, list[dict[str, Any]]]:
    schema = json.loads(SCHEMA_PATH.read_text())
    package = {}
    for filename, shape in schema["$defs"]["files"]["properties"].items():
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("fixture path escapes package")
        source = path.read_text(encoding="utf-8")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        def parse(value):
            return json.loads(value, object_pairs_hook=unique)
        entries = [parse(line) for line in source.splitlines() if line.strip()] if filename.endswith("jsonl") else parse(source)
        validator = Draft202012Validator({"$defs": schema["$defs"], **shape}, format_checker=FormatChecker())
        if filename.endswith("jsonl"):
            for entry in entries:
                validator.validate(entry)
        else:
            validator.validate(entries)
            if filename == "family.json":
                entries = [entries]
        package[filename] = entries
    family = package["family.json"][0]
    scope = (family["package_id"], family["family_ref"])
    if not family["gold"]["value"]["synthetic"]:
        raise ValueError("fixture importer requires explicitly synthetic data")
    if any(a["kind"] != "PACKAGE" or a["source_ref"] != family["family_ref"] for a in family["gold"]["evidence_refs"]):
        raise ValueError("family identity declarations must be explicit package metadata, not extracted private records")
    if any(p["value"] != family["gold"]["value"] for p in family["predicted"]):
        raise ValueError("family identity disagreement needs review")
    if family["external_dataset_id"] != family["family_ref"]:
        raise ValueError("family_ref does not identify family")
    for filename, entries in package.items():
        seen = set()
        for entry in entries:
            if (entry["package_id"], entry["family_ref"]) != scope or entry["external_dataset_id"] in seen:
                raise ValueError("invalid/duplicate external scope")
            seen.add(entry["external_dataset_id"])
            gold = entry["gold"]
            if gold["annotation_status"] != "ADJUDICATED" or not gold["annotator_ids"] or not gold["evidence_refs"]:
                raise ValueError("fixture import requires adjudicated annotations and evidence")
    return package


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _time(value: dict[str, Any]) -> dict[str, Any]:
    return TemporalValue.model_validate(value).model_dump()


def import_fixture(db: Session, directory: Path, *, room_id: str, actor_user_id: str,
                   member_bindings: dict[str, str], record_bindings: dict[str, str]) -> dict[str, dict[str, str]]:
    """Atomic caller-owned transaction; never commit or create accounts/records.

    ADMIN may import explicitly reviewed commons context, but cannot bind a
    PRIVATE source unless M2 grants that actor access. All source bindings retain
    current version and ACL; read projections recheck them before materialization.
    """
    mode = db.execute(text("SELECT mode FROM security_control WHERE id=1")).scalar_one_or_none()
    if mode not in (None, "LEGACY"):
        raise ValueError("M3 fixture graph requires isolated LEGACY state; encrypted context adapter is not enabled")
    actor = membership(db, actor_user_id, room_id)
    if actor.role.lower() not in ("owner", "admin"):
        raise unavailable()
    package = load_package(directory)
    family = package["family.json"][0]
    package_id, family_ref = family["package_id"], family["family_ref"]
    mappings: dict[str, dict[str, str]] = {}

    def bind(kind, external, value, runtime=None):
        table = mappings.setdefault(kind, {})
        if external in table:
            raise ValueError("duplicate external identifier")
        row = db.scalar(select(ExternalDatasetRef).where(ExternalDatasetRef.room_id == room_id,
            ExternalDatasetRef.package_id == package_id, ExternalDatasetRef.family_ref == family_ref,
            ExternalDatasetRef.kind == kind, ExternalDatasetRef.external_dataset_id == external))
        digest = _digest(value)
        if row:
            if row.content_sha256 != digest or (runtime is not None and runtime != row.runtime_id):
                raise ValueError("external mapping conflict; explicit contract/import revision required")
            table[external] = row.runtime_id
            return row.runtime_id, False
        identity = runtime or str(uuid4())
        db.add(ExternalDatasetRef(id=str(uuid4()), room_id=room_id, package_id=package_id,
            family_ref=family_ref, kind=kind, external_dataset_id=external, runtime_id=identity, content_sha256=digest))
        table[external] = identity
        return identity, True

    def ref(kind, key):
        try:
            return mappings[kind][key]
        except KeyError as exc:
            raise ValueError("unresolved scoped dataset reference") from exc

    def source_records(entry, additional=()):
        refs = list(additional)
        for anchor in entry["gold"]["evidence_refs"]:
            if anchor["kind"] != "PACKAGE":
                refs.append(anchor["source_ref"])
            elif anchor["source_ref"] != family_ref:
                raise ValueError("unresolved package evidence")
        return sorted({ref("RECORD", key) for key in refs})

    def context(entry, kind, extra=()):
        records = source_records(entry, extra)
        if kind in ("EVENT", "PARTICIPATION") and not records:
            raise ValueError("events/participation require record evidence")
        identity, new = bind(kind, entry["external_dataset_id"], entry)
        if new:
            disagreement = any(p["value"] != entry["gold"]["value"] for p in entry["predicted"])
            db.add(ContextEntity(id=identity, room_id=room_id, kind=kind,
                review_status="NEEDS_REVIEW" if disagreement else "APPROVED"))
            db.flush()
            for record_id in records:
                row = require_record_access(db, actor_user_id, record_id, room_id=room_id, purpose="rag")
                db.add(ContextSource(entity_id=identity, record_id=record_id,
                    record_version=row["record_version"], source_revision=row["source_revision"]))
        return identity, new

    with db.begin_nested():
        bind("FAMILY", family_ref, family, room_id)
        people = family["gold"]["value"]["persons"]
        if len({p["external_dataset_id"] for p in people}) != len(people):
            raise ValueError("duplicate person external ID")
        if set(member_bindings) - {p["external_dataset_id"] for p in people}:
            raise ValueError("unknown person binding")
        for person in people:
            external = person["external_dataset_id"]
            member_id = member_bindings.get(external)
            user_id = None
            if person["account_fixture_ref"] is not None:
                member = db.scalar(select(FamilyMember).where(FamilyMember.id == member_id,
                    FamilyMember.room_id == room_id, FamilyMember.is_active.is_(True)))
                if member is None:
                    raise unavailable()
                user_id = member.user_id
            elif member_id is not None:
                raise ValueError("non-account person cannot gain membership from import")
            existing = db.scalar(select(FamilyPerson).where(FamilyPerson.room_id == room_id, FamilyPerson.user_id == user_id)) if user_id else None
            identity, new = bind("PERSON", external, person, existing.id if existing else None)
            if new and not existing:
                if person["display_name"] is None:
                    raise ValueError("existing person storage requires an explicitly supplied display label")
                from datetime import date
                db.add(FamilyPerson(id=identity, room_id=room_id, user_id=user_id,
                    display_name=person["display_name"], birth_date=date.fromisoformat(person["birth_date"]) if person["birth_date"] else None))
            db.flush()
        record_entries = {x["external_dataset_id"]: x for x in package["records.jsonl"]}
        if set(record_entries) != set(record_bindings):
            raise ValueError("record bindings must match supplied record entries")
        for external, runtime in record_bindings.items():
            row = require_record_access(db, actor_user_id, runtime, room_id=room_id, purpose="rag")
            gold = record_entries[external]["gold"]["value"]
            owner = member_bindings.get(gold["owner_member_id"])
            if owner != row["owner_member_id"] or gold["visibility"] != row["visibility"] or gold["lifecycle"] != row["lifecycle_state"] or gold["review_state"] != row["review_status"]:
                raise ValueError("record binding does not match current authorized fixture state")
            _time(gold["created_at"])
            bind("RECORD", external, record_entries[external], runtime)
        for entry in package["relations.json"]:
            value = entry["gold"]["value"]
            identity, new = context(entry, "RELATION")
            start, end = _time(value["valid_from"]), _time(value["valid_to"])
            source, target = ref("PERSON", value["from_member_id"]), ref("PERSON", value["to_member_id"])
            if source == target:
                raise ValueError("self relation")
            if value["relation"] in ("SPOUSE_OF", "SIBLING_OF"):
                source, target = sorted((source, target))
            if new:
                db.add(FamilyRelation(id=identity, room_id=room_id, from_person_id=source, to_person_id=target,
                    relation_type=value["relation"], valid_from=start, valid_to=end))
        db.flush()
        # Reject parent cycles across both old and newly imported M3 relations.
        graph = {}
        for edge in db.scalars(select(FamilyRelation).where(FamilyRelation.room_id == room_id, FamilyRelation.relation_type == "PARENT_OF")):
            graph.setdefault(edge.from_person_id, set()).add(edge.to_person_id)
        def walk(node, stack):
            if node in stack:
                raise ValueError("parent relation cycle")
            for child in graph.get(node, ()):
                walk(child, stack | {node})
        for node in graph:
            walk(node, set())
        for entry in package["aliases.json"]:
            value = entry["gold"]["value"]
            identity, new = context(entry, "ALIAS")
            if value["speaker_member_id"] is None:
                raise ValueError("M3 aliases require an explicit speaker")
            speaker = ref("PERSON", value["speaker_member_id"])
            viewer = ref("PERSON", value["viewer_member_id"]) if value["viewer_member_id"] else None
            targets = value["candidate_member_ids"] if value["resolution"] == "AMBIGUOUS" else [value["target_member_id"]] if value["resolution"] == "UNIQUE" else []
            if value["resolution"] == "AMBIGUOUS" and len(set(targets)) < 2:
                raise ValueError("ambiguous alias requires multiple targets")
            if new:
                db.add(RelationAlias(id=identity, room_id=room_id, speaker_person_id=speaker,
                    viewer_person_id=viewer, alias=value["text"], valid_from=_time(value["valid_from"]), valid_to=_time(value["valid_to"])))
                db.flush()
                for target in set(targets):
                    db.add(AliasTarget(alias_id=identity, room_id=room_id, person_id=ref("PERSON", target)))
        # Topological parent ordering is explicit; no guessed event hierarchy.
        pending = list(package["events.jsonl"])
        while pending:
            progressed = False
            for entry in pending[:]:
                value = entry["gold"]["value"]
                parent = value["parent_event_id"]
                if parent and parent not in mappings.get("EVENT", {}):
                    continue
                identity, new = context(entry, "EVENT", value["source_record_ids"])
                if new:
                    db.add(Event(id=identity, room_id=room_id, title=value["title"], category=value["category"],
                        event_status=value["event_status"], occurred_at=_time(value["occurred_at"]), location=value["location"],
                        parent_event_id=ref("EVENT", parent) if parent else None, phase_ref=value["phase_ref"], exam_id=value["exam_id"]))
                    db.flush()
                pending.remove(entry)
                progressed = True
            if not progressed:
                raise ValueError("cyclic or missing parent event")
        for entry in package["event_participation.jsonl"]:
            value = entry["gold"]["value"]
            identity, new = context(entry, "PARTICIPATION")
            event, person = ref("EVENT", value["event_id"]), ref("PERSON", value["member_id"])
            phase = value["phase_ref"] or ""
            if value["mode"] in ("ONSITE", "NOT_PRESENT"):
                opposite = "NOT_PRESENT" if value["mode"] == "ONSITE" else "ONSITE"
                if db.scalar(select(EventParticipation.id).where(EventParticipation.event_id == event,
                    EventParticipation.person_id == person, EventParticipation.phase_ref == phase, EventParticipation.mode == opposite)):
                    raise ValueError("contradictory attendance")
            if new:
                db.add(EventParticipation(id=identity, room_id=room_id, event_id=event, person_id=person,
                    phase_ref=phase, mode=value["mode"], occurred_at=_time(value["occurred_at"]), activity=value["activity"]))
                db.flush()
        db.flush()
    return mappings


def resolve_external(db: Session, *, user_id: str, room_id: str, package_id: str,
                     family_ref: str, kind: str, external_dataset_id: str) -> str:
    """Resolve only within explicit scope and current viewer authorization."""
    from app.services.family_context import entity_ids, visible_people

    membership(db, user_id, room_id)
    row = db.scalar(select(ExternalDatasetRef).where(
        ExternalDatasetRef.room_id == room_id, ExternalDatasetRef.package_id == package_id,
        ExternalDatasetRef.family_ref == family_ref, ExternalDatasetRef.kind == kind,
        ExternalDatasetRef.external_dataset_id == external_dataset_id))
    if row is None:
        raise unavailable()
    if kind == "RECORD":
        require_record_access(db, user_id, row.runtime_id, room_id=room_id, purpose="rag")
    elif kind == "PERSON":
        if db.scalar(visible_people(db, room_id).where(FamilyPerson.id == row.runtime_id)) is None:
            raise unavailable()
    elif kind in ("RELATION", "ALIAS", "EVENT", "PARTICIPATION"):
        if db.scalar(entity_ids(db, user_id, room_id, kind).where(ContextEntity.id == row.runtime_id)) is None:
            raise unavailable()
    elif kind != "FAMILY":
        raise unavailable()
    return row.runtime_id
