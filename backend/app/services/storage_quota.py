"""Atomic room quota and durable LEGACY local-storage allocation lifecycle."""
from __future__ import annotations

import uuid
import logging
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.deletion_intent import DeletionIntent
from app.models.family import FamilyRoom
from app.models.storage_allocation import LegacyStorageAllocation
from app.models.upload import Upload, UploadFile

COUNTED_STATES = ("RESERVED", "ACTIVE", "DELETE_PENDING")
RESERVATION_TTL = timedelta(minutes=10)
RECOVERY_BATCH_LIMIT = 50
logger = logging.getLogger(__name__)


class RoomStorageQuotaExceeded(ValueError):
    pass


class StorageAllocationStateChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class StorageReservation:
    id: str
    room_id: str
    upload_id: str
    storage_bucket: str
    storage_path: str
    byte_size: int


def _lock_room(db: Session, room_id: str, *, require_active: bool = True) -> None:
    """Canonical mutation order: room first, then allocation/record rows.

    Every publication, recovery and deletion takes this same room lock before
    touching either child; multi-room deletion locks rooms in sorted order.
    """
    dialect = db.get_bind().dialect.name
    predicate = (FamilyRoom.id == room_id) & (FamilyRoom.is_active.is_(True) if require_active else True)
    if dialect == "postgresql":
        found = db.scalar(
            select(FamilyRoom.id).where(predicate).with_for_update()
        )
    else:
        found = room_id if db.execute(
            update(FamilyRoom)
            .where(predicate)
            .values(id=FamilyRoom.id)
            .execution_options(synchronize_session=False)
        ).rowcount == 1 else None
    if found is None:
        raise StorageAllocationStateChanged("Family room disappeared")


def _room_usage(db: Session, room_id: str, quota: int) -> int:
    allocated = db.scalar(
        select(func.coalesce(func.sum(LegacyStorageAllocation.byte_size), 0)).where(
            LegacyStorageAllocation.room_id == room_id,
            LegacyStorageAllocation.state.in_(COUNTED_STATES),
        )
    )
    # Fail-safe compatibility for tests or an explicitly pre-migration database:
    # production backfill links every local row before 0011 is stamped.
    unlinked = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (UploadFile.file_size.is_(None), quota),
                        else_=UploadFile.file_size,
                    )
                ),
                0,
            )
        )
        .select_from(UploadFile)
        .join(Upload, Upload.id == UploadFile.upload_id)
        .where(
            Upload.room_id == room_id,
            UploadFile.storage_bucket == "local-media",
            UploadFile.storage_allocation_id.is_(None),
        )
    )
    return int(allocated or 0) + int(unlinked or 0)


def reserve(
    db: Session,
    *,
    room_id: str,
    upload_id: str,
    byte_size: int,
    suffix: str,
    now: datetime | None = None,
) -> StorageReservation:
    quota = settings.legacy_room_storage_quota_bytes
    if type(byte_size) is not int or byte_size <= 0 or type(quota) is not int or quota <= 0:
        raise StorageAllocationStateChanged("Invalid storage accounting input")
    if Path(suffix).name != suffix or not suffix.startswith(".") or len(suffix) > 16:
        raise StorageAllocationStateChanged("Invalid storage suffix")
    _lock_room(db, room_id)
    upload = db.get(Upload, upload_id)
    if upload is None or upload.room_id != room_id:
        raise StorageAllocationStateChanged("Upload disappeared")
    if _room_usage(db, room_id, quota) + byte_size > quota:
        raise RoomStorageQuotaExceeded()
    allocation_id = str(uuid.uuid4())
    timestamp = now or datetime.now(timezone.utc)
    storage_path = f"{upload_id}__{allocation_id}{suffix}"
    allocation = LegacyStorageAllocation(
        id=allocation_id,
        room_id=room_id,
        upload_id=upload_id,
        storage_bucket="local-media",
        storage_path=storage_path,
        byte_size=byte_size,
        state="RESERVED",
        reserved_at=timestamp,
        expires_at=timestamp + RESERVATION_TTL,
        updated_at=timestamp,
    )
    db.add(allocation)
    db.commit()
    return StorageReservation(
        allocation.id,
        allocation.room_id,
        allocation.upload_id,
        allocation.storage_bucket,
        allocation.storage_path,
        allocation.byte_size,
    )


@contextmanager
def _local_publication_lock(*, blocking=True):
    # ponytail: one lock per local media root; use per-allocation locks if write throughput matters.
    # Keep this inode forever, outside media cleanup. OS ownership survives lost DB sessions.
    from app.api.routes.uploads import LOCAL_MEDIA_ROOT
    root = LOCAL_MEDIA_ROOT.resolve()
    path = root.with_name("." + root.name + ".publication.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or path.is_symlink():
            raise StorageAllocationStateChanged("Invalid publication lock")
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                if blocking:
                    raise
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                locked = True
            except BlockingIOError:
                pass
        yield locked
    finally:
        try:
            if locked:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def publish_local(session_factory, *, allocation_id, storage_path, byte_size, writer):
    """Fence local create with OS ownership, then the canonical room DB lock.

    Validate under the room lock, then close SQL before physical I/O. OS
    ownership keeps RESERVED charged until the write ends or the process dies,
    even if the room is deleted. Recovery takes the same OS lock. No remote I/O.
    """
    with _local_publication_lock():
        db = session_factory()
        try:
            allocation = db.get(LegacyStorageAllocation, allocation_id)
            if allocation is None:
                raise StorageAllocationStateChanged("Storage reservation missing")
            _lock_room(db, allocation.room_id)
            db.refresh(allocation)
            expires = allocation.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if (allocation.state != "RESERVED" or expires <= datetime.now(timezone.utc)
                    or allocation.storage_bucket != "local-media"
                    or allocation.storage_path != storage_path
                    or allocation.byte_size != byte_size
                    or db.get(Upload, allocation.upload_id) is None):
                raise StorageAllocationStateChanged("Storage writer fenced")
        finally:
            db.rollback()
            db.close()
        return writer()


def _intent_for_allocation(db: Session, allocation: LegacyStorageAllocation) -> str:
    existing = db.scalar(
        select(DeletionIntent.id).where(
            DeletionIntent.storage_allocation_id == allocation.id
        )
    )
    if existing is not None:
        return existing
    intent = DeletionIntent(
        id=str(uuid.uuid4()),
        scope="record",
        scope_id=allocation.upload_id,
        storage_bucket=allocation.storage_bucket,
        storage_path=allocation.storage_path,
        storage_allocation_id=allocation.id,
        status="pending",
    )
    db.add(intent)
    return intent.id


def _referencing_file(
    db: Session, reservation: StorageReservation | LegacyStorageAllocation
) -> UploadFile | None:
    return db.scalar(
        select(UploadFile)
        .where(
            (UploadFile.storage_allocation_id == reservation.id)
            | (
                (UploadFile.storage_bucket == reservation.storage_bucket)
                & (UploadFile.storage_path == reservation.storage_path)
            )
        )
        .limit(1)
    )


def _reconcile_referenced_reservation(
    db: Session, reservation: StorageReservation | LegacyStorageAllocation
) -> list[str] | None:
    """Adopt a current publication, or retain a charged nonretryable intent."""
    upload_file = _referencing_file(db, reservation)
    if upload_file is None:
        return None
    latest = db.scalar(
        select(UploadFile.id)
        .where(UploadFile.upload_id == upload_file.upload_id)
        .order_by(UploadFile.created_at.desc(), UploadFile.id.desc())
        .limit(1)
    )
    if upload_file.storage_allocation_id not in (None, reservation.id):
        owner = db.get(
            LegacyStorageAllocation, upload_file.storage_allocation_id
        )
        if (
            latest == upload_file.id
            and owner is not None
            and owner.state == "ACTIVE"
            and owner.room_id == reservation.room_id
            and owner.storage_bucket == reservation.storage_bucket
            and owner.storage_path == reservation.storage_path
            and owner.byte_size == reservation.byte_size
        ):
            _lock_room(db, reservation.room_id)
            released = db.execute(
                update(LegacyStorageAllocation)
                .where(
                    LegacyStorageAllocation.id == reservation.id,
                    LegacyStorageAllocation.state == "RESERVED",
                )
                .values(state="RELEASED", updated_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            ).rowcount
            if released != 1:
                raise StorageAllocationStateChanged(
                    "Storage allocation state changed"
                )
            return []
    if (
        upload_file.upload_id == reservation.upload_id
        and latest == upload_file.id
        and upload_file.storage_allocation_id in (None, reservation.id)
        and upload_file.file_size == reservation.byte_size
    ):
        _lock_room(db, reservation.room_id)
        changed = db.execute(
            update(LegacyStorageAllocation)
            .where(
                LegacyStorageAllocation.id == reservation.id,
                LegacyStorageAllocation.state == "RESERVED",
            )
            .values(state="ACTIVE", updated_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed != 1:
            raise StorageAllocationStateChanged("Storage allocation state changed")
        if upload_file.storage_allocation_id is None:
            linked = db.execute(
                update(UploadFile)
                .where(
                    UploadFile.id == upload_file.id,
                    UploadFile.storage_allocation_id.is_(None),
                )
                .values(storage_allocation_id=reservation.id)
                .execution_options(synchronize_session=False)
            ).rowcount
            if linked != 1:
                raise StorageAllocationStateChanged("Storage publication link changed")
        return []

    intent_id = mark_delete_pending(db, reservation.id, expected_state="RESERVED")
    db.execute(
        update(DeletionIntent)
        .where(DeletionIntent.id == intent_id)
        .values(status="failed", failure_code="locator_referenced")
    )
    return [intent_id]


def mark_delete_pending(
    db: Session, allocation_id: str, *, expected_state: str
) -> str:
    changed = db.execute(
        update(LegacyStorageAllocation)
        .where(
            LegacyStorageAllocation.id == allocation_id,
            LegacyStorageAllocation.state == expected_state,
        )
        .values(state="DELETE_PENDING", updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise StorageAllocationStateChanged("Storage allocation state changed")
    db.expire_all()
    allocation = db.get(LegacyStorageAllocation, allocation_id)
    if allocation is None:
        raise StorageAllocationStateChanged("Storage allocation disappeared")
    return _intent_for_allocation(db, allocation)


def activate(
    db: Session,
    reservation: StorageReservation,
    *,
    upload_file: UploadFile,
) -> list[str]:
    _lock_room(db, reservation.room_id)
    upload_exists = db.scalar(
        select(Upload.id).where(
            Upload.id == reservation.upload_id,
            Upload.room_id == reservation.room_id,
        )
    )
    if upload_exists is None:
        raise StorageAllocationStateChanged("Upload disappeared")
    previous_created_at = db.scalar(
        select(func.max(UploadFile.created_at)).where(
            UploadFile.upload_id == reservation.upload_id
        )
    )
    activated_at = datetime.now(timezone.utc)
    if previous_created_at is not None:
        if previous_created_at.tzinfo is None:
            activated_at = activated_at.replace(tzinfo=None)
        activated_at = max(
            activated_at,
            previous_created_at + timedelta(microseconds=1),
        )
    upload_file.created_at = activated_at
    changed = db.execute(
        update(LegacyStorageAllocation)
        .where(
            LegacyStorageAllocation.id == reservation.id,
            LegacyStorageAllocation.state == "RESERVED",
            LegacyStorageAllocation.room_id == reservation.room_id,
            LegacyStorageAllocation.upload_id == reservation.upload_id,
            LegacyStorageAllocation.storage_bucket == reservation.storage_bucket,
            LegacyStorageAllocation.storage_path == reservation.storage_path,
            LegacyStorageAllocation.byte_size == reservation.byte_size,
        )
        .values(state="ACTIVE", updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise StorageAllocationStateChanged("Storage reservation is no longer active")
    previous = db.scalars(
        select(LegacyStorageAllocation).where(
            LegacyStorageAllocation.upload_id == reservation.upload_id,
            LegacyStorageAllocation.state == "ACTIVE",
            LegacyStorageAllocation.id != reservation.id,
        )
    ).all()
    intent_ids = [
        mark_delete_pending(db, item.id, expected_state="ACTIVE") for item in previous
    ]
    upload_file.storage_allocation_id = reservation.id
    db.add(upload_file)
    return intent_ids


def abandon(session_factory, reservation: StorageReservation) -> list[str]:
    db = session_factory()
    try:
        try:
            if db.get(FamilyRoom, reservation.room_id) is not None:
                _lock_room(db, reservation.room_id, require_active=False)
            reconciled = _reconcile_referenced_reservation(db, reservation)
            intent_ids = (
                reconciled
                if reconciled is not None
                else [
                    mark_delete_pending(
                        db, reservation.id, expected_state="RESERVED"
                    )
                ]
            )
            db.commit()
            return intent_ids
        except StorageAllocationStateChanged:
            db.rollback()
            return []
        except Exception as error:
            db.rollback()
            logger.warning(
                "Storage reservation cleanup deferred: %s",
                type(error).__name__,
            )
            return []
    finally:
        db.close()


def recover_expired(session_factory, *, now: datetime | None = None) -> list[str]:
    # A stalled writer must retain its charged reservation; recovery never waits.
    with _local_publication_lock(blocking=False) as acquired:
        if not acquired:
            return []
        return _recover_expired_locked(session_factory, now=now)


def _recover_expired_locked(session_factory, *, now: datetime | None = None) -> list[str]:
    timestamp = now or datetime.now(timezone.utc)
    db = session_factory()
    intent_ids: list[str] = []
    try:
        ids = db.scalars(
            select(LegacyStorageAllocation.id)
            .where(
                LegacyStorageAllocation.state == "RESERVED",
                LegacyStorageAllocation.expires_at <= timestamp,
            )
            .order_by(LegacyStorageAllocation.expires_at, LegacyStorageAllocation.id)
            .limit(RECOVERY_BATCH_LIMIT)
        ).all()
        for allocation_id in ids:
            allocation = db.get(LegacyStorageAllocation, allocation_id)
            if allocation is None:
                continue
            try:
                if db.get(FamilyRoom, allocation.room_id) is not None:
                    _lock_room(db, allocation.room_id, require_active=False)
                db.refresh(allocation)
                if allocation.state != "RESERVED":
                    db.rollback()
                    continue
                reconciled = _reconcile_referenced_reservation(db, allocation)
                if reconciled is None:
                    intent_ids.append(
                        mark_delete_pending(
                            db, allocation_id, expected_state="RESERVED"
                        )
                    )
                else:
                    intent_ids.extend(reconciled)
                db.commit()
            except StorageAllocationStateChanged:
                db.rollback()
                continue
        db.commit()
        return intent_ids
    finally:
        db.close()


def source_is_current_active(db: Session, upload_file: UploadFile) -> bool:
    latest = db.scalar(
        select(UploadFile.id)
        .where(UploadFile.upload_id == upload_file.upload_id)
        .order_by(UploadFile.created_at.desc(), UploadFile.id.desc())
        .limit(1)
    )
    if latest != upload_file.id:
        return False
    if upload_file.storage_bucket != "local-media":
        return True
    if upload_file.storage_allocation_id is None:
        return False
    state = db.scalar(
        select(LegacyStorageAllocation.state).where(
            LegacyStorageAllocation.id == upload_file.storage_allocation_id
        )
    )
    return state == "ACTIVE"
