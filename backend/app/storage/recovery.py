"""Bounded recovery manifest contract only; no export, persistence or age process.

Package A exercises these pure functions exclusively with public synthetic keys.
Real backup ceremony, unlock custody, tombstone reconciliation and retention are
separately gated. Original independent keys, not host-encrypted files, are entries.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from uuid import UUID

from .contracts import KeyRef, positive_int, secret_bytes, uuid_value

MAX_MANIFEST_SIZE = 1024 * 1024
MAX_ENTRIES = 1000


@dataclass(frozen=True)
class RecoveryEntry:
    ref: KeyRef
    key: bytes = field(repr=False)
    state: str


@dataclass(frozen=True)
class RecoveryManifest:
    inventory_id: UUID
    created_at: datetime
    inventory_revision: UUID
    recovery_epoch: int
    entries: tuple[RecoveryEntry, ...] = field(repr=False)


def encode_manifest(manifest):
    if type(manifest) is not RecoveryManifest:
        raise ValueError("Recovery manifest required")
    uuid_value(manifest.inventory_id)
    uuid_value(manifest.inventory_revision)
    positive_int(manifest.recovery_epoch)
    if (not isinstance(manifest.created_at, datetime) or manifest.created_at.tzinfo is None
            or manifest.created_at.microsecond != 0 or type(manifest.entries) is not tuple
            or not 1 <= len(manifest.entries) <= MAX_ENTRIES):
        raise ValueError("Invalid recovery manifest")
    rows, seen = [], set()
    for entry in manifest.entries:
        if type(entry) is not RecoveryEntry or type(entry.ref) is not KeyRef or entry.state not in ("active", "retired"):
            raise ValueError("Ineligible recovery entry")
        if entry.ref in seen:
            raise ValueError("Duplicate recovery key")
        seen.add(entry.ref)
        secret_bytes(entry.key, 32)
        rows.append([entry.ref.domain, str(entry.ref.owner_id), str(entry.ref.key_id), entry.ref.epoch, entry.state, entry.key.hex()])
    raw = json.dumps(["ambient-legacy/recovery", 1, str(manifest.inventory_id),
        manifest.created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        str(manifest.inventory_revision), manifest.recovery_epoch, rows], separators=(",", ":")).encode()
    if len(raw) > MAX_MANIFEST_SIZE:
        raise ValueError("Recovery manifest capacity exceeded")
    return raw


def decode_manifest(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_MANIFEST_SIZE:
        raise ValueError("Invalid recovery manifest")
    try:
        value = json.loads(raw)
        if (type(value) is not list or len(value) != 7 or value[0] != "ambient-legacy/recovery"
                or type(value[1]) is not int or value[1] != 1 or type(value[6]) is not list
                or not 1 <= len(value[6]) <= MAX_ENTRIES):
            raise ValueError()
        entries = []
        for row in value[6]:
            if type(row) is not list or len(row) != 6 or type(row[5]) is not str or len(row[5]) != 64:
                raise ValueError()
            entries.append(RecoveryEntry(KeyRef(row[0], UUID(row[1]), UUID(row[2]), row[3]), bytes.fromhex(row[5]), row[4]))
        manifest = RecoveryManifest(UUID(value[2]), datetime.strptime(value[3], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
                                    UUID(value[4]), value[5], tuple(entries))
        if encode_manifest(manifest) != raw:
            raise ValueError()
        return manifest
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("Invalid recovery manifest") from None
