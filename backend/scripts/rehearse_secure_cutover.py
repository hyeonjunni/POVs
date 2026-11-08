"""B6.6/B6.7 marker-guarded synthetic cutover rehearsal."""

import json
import os
import sqlite3
import stat
from pathlib import Path
from uuid import UUID, uuid4


_MARKER = b"B6 disposable fixture v1\n"
_CHECKPOINT_KEYS = {"catalogue_id", "control_revision", "inventory_revisions", "deletion_epochs"}
_INVENTORY_KEYS = {"version", "catalogue_id", "targets"}
_TARGET_KEYS = {"owner_type", "owner_id"}
_CONTROL_COLUMNS = ("id", "mode", "schema_version", "revision", "catalogue_id", "runtime_generation", "admission")
_CONTROL_FENCES = tuple(f"ck_m4_security_control_{number}" for number in range(1, 7))


def _reject() -> None:
    raise ValueError("B6.6/B6.7 synthetic rehearsal rejected")


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _synthetic_root(root: object) -> int:
    if not isinstance(root, Path) or not root.is_absolute() or not hasattr(os, "O_NOFOLLOW"):
        _reject()
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        _reject()
    try:
        root_status = os.fstat(descriptor)
        marker = os.open(".ambient-m4-synthetic", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(marker, "rb") as source:
            marker_status = os.fstat(source.fileno())
            marker_value = source.read()
    except OSError:
        os.close(descriptor)
        _reject()
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_mode & 0o077
        or root_status.st_uid != os.geteuid()
        or not stat.S_ISREG(marker_status.st_mode)
        or marker_value != _MARKER
    ):
        os.close(descriptor)
        _reject()
    return descriptor


def _checkpoint_matches(value: object, catalogue_id: object, revision: object) -> bool:
    if type(value) is not dict or set(value) != _CHECKPOINT_KEYS:
        return False
    return (
        _canonical_uuid(value["catalogue_id"])
        and value["catalogue_id"] == catalogue_id
        and type(value["control_revision"]) is int
        and value["control_revision"] == revision
        and value["inventory_revisions"] == []
        and value["deletion_epochs"] == []
    )


def _duplicate_free_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _reject()
        value[key] = item
    return value


def _inventory(root_descriptor: int) -> tuple[str, set[str]]:
    try:
        descriptor = os.open(
            "synthetic-inventory.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_descriptor
        )
        with os.fdopen(descriptor, encoding="utf-8") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                _reject()
            inventory = json.load(source, object_pairs_hook=_duplicate_free_object)
    except (OSError, json.JSONDecodeError):
        _reject()
    if type(inventory) is not dict or set(inventory) != _INVENTORY_KEYS:
        _reject()
    if type(inventory["version"]) is not int or inventory["version"] != 1:
        _reject()
    if not _canonical_uuid(inventory["catalogue_id"]) or type(inventory["targets"]) is not list:
        _reject()
    targets: set[str] = set()
    for target in inventory["targets"]:
        if type(target) is not dict or set(target) != _TARGET_KEYS:
            _reject()
        owner_id = target["owner_id"]
        if target["owner_type"] != "user" or not _canonical_uuid(owner_id) or owner_id in targets:
            _reject()
        targets.add(owner_id)
    if not targets:
        _reject()
    return inventory["catalogue_id"], targets


def _database_uri(root_descriptor: int) -> tuple[str, str]:
    descriptor = -1
    alias = f".ambient-m4-catalogue-{os.getpid()}-{uuid4().hex}"
    try:
        descriptor = os.open(
            "catalogue.sqlite", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_descriptor
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _reject()
        os.link(f"/proc/self/fd/{descriptor}", alias, dst_dir_fd=root_descriptor)
    except OSError:
        _reject()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return f"file:/proc/self/fd/{root_descriptor}/{alias}?mode=rw", alias


def _canonical_transition_trigger() -> str:
    from importlib.util import module_from_spec, spec_from_file_location

    migration = Path(__file__).parents[1] / "alembic" / "versions" / "0005_secure_catalogue.py"
    spec = spec_from_file_location("_b6_secure_catalogue", migration)
    if spec is None or spec.loader is None:
        _reject()
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return next(sql for sql in module.trigger_statements() if sql.startswith("CREATE TRIGGER m4_control_transition "))


def _normalized_sql(value: str) -> str:
    return "".join(value.split())


def _control_schema_is_expected(database: sqlite3.Connection) -> bool:
    master = database.execute(
        "SELECT type, sql FROM sqlite_master WHERE name='security_control'"
    ).fetchall()
    columns = tuple(row[1] for row in database.execute("PRAGMA table_info(security_control)"))
    trigger = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='m4_control_transition'"
    ).fetchall()
    return (
        len(master) == 1
        and master[0][0] == "table"
        and columns == _CONTROL_COLUMNS
        and all(fence in master[0][1] for fence in _CONTROL_FENCES)
        and len(trigger) == 1
        and _normalized_sql(trigger[0][0]) == _normalized_sql(_canonical_transition_trigger())
    )


def _quarantine(database: sqlite3.Connection) -> None:
    updated = database.execute(
        "UPDATE security_control SET revision=revision+1, mode='QUARANTINED', admission='CLOSED' WHERE id=1"
    ).rowcount
    if updated != 1:
        _reject()


def resume_synthetic_cutover(*, root: Path, engine: object, store: object, checkpoint: object) -> None:
    """Accept only current synthetic authority; quarantine stale authority."""
    del engine, store
    root_descriptor = _synthetic_root(root)
    alias: str | None = None
    try:
        database_uri, alias = _database_uri(root_descriptor)
        with sqlite3.connect(database_uri, uri=True) as database:
            if not _control_schema_is_expected(database):
                _reject()
            controls = database.execute(
                "SELECT catalogue_id, revision, mode, admission, schema_version FROM security_control"
            ).fetchall()
            users = {row[0] for row in database.execute("SELECT id FROM users")}
            inventory_count = database.execute("SELECT COUNT(*) FROM secure_deletion_inventory").fetchone()[0]
            tombstone_count = database.execute("SELECT COUNT(*) FROM security_tombstones").fetchone()[0]
            inventory_catalogue, targets = _inventory(root_descriptor)
            if len(controls) != 1:
                _reject()
            control = controls[0]
            if not _canonical_uuid(control[0]) or type(control[1]) is not int or control[4] != 1:
                _reject()
            if control[2:4] == ("QUARANTINED", "CLOSED"):
                return
            current = (
                (control[2], control[3], control[4]) == ("CUTOVER", "CLOSED", 1)
                and control[0] == inventory_catalogue
                and users == targets
                and inventory_count == 0
                and tombstone_count == 0
            )
            if current and _checkpoint_matches(checkpoint, control[0], control[1]):
                return
            _quarantine(database)
    except (OSError, sqlite3.Error):
        _reject()
    finally:
        if alias is not None:
            try:
                os.unlink(alias, dir_fd=root_descriptor)
            except OSError:
                _reject()
        os.close(root_descriptor)
