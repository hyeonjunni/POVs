"""Persist DB deletion first, then drive durable storage-deletion intents."""
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.api.routes.uploads import LOCAL_MEDIA_ROOT
from app.core.config import settings
from app.models.auth_identity import AuthIdentity
from app.models.deletion_intent import DeletionIntent
from app.models.family import FamilyMember, FamilyRoom
from app.models.processing_job import ProcessingJob
from app.models.record import Record, RecordAudit
from app.models.retrieval import RecordChunk, RecordEmbedding
from app.models.storage_allocation import LegacyStorageAllocation
from app.models.upload import Upload, UploadFile
from app.models.user import User
from app.processing.contracts import ProviderError
from app.services.media_loader import resolve_trusted_locator
from app.services.storage_quota import _lock_room

logger = logging.getLogger(__name__)

# ponytail: one bounded recovery batch per request; schedule a sweep only if backlog forms.
INTENT_BATCH_LIMIT = 200


class _DeletionControlError(RuntimeError):
    """Owned, stable deletion refusal code safe to persist."""


def get_gcs_client():
    """Compatibility seam: LEGACY never constructs a remote storage client."""
    return None


def delete_gcs_blob(*_args, **_kwargs):
    """Compatibility seam: LEGACY remote deletion is permanently unsupported."""
    raise _DeletionControlError("historical_storage_unsupported")


def write_deletion_intents(
    db: Session, *, scope: str, scope_id: str, upload_files: Sequence[UploadFile]
) -> list[str]:
    ids: list[str] = []
    for upload_file in upload_files:
        if upload_file.storage_allocation_id:
            allocation = db.get(
                LegacyStorageAllocation, upload_file.storage_allocation_id
            )
            if allocation is None:
                raise _DeletionControlError("allocation_missing")
            if allocation.state == "RELEASED":
                continue
            existing = db.scalar(
                select(DeletionIntent.id).where(
                    DeletionIntent.storage_allocation_id == allocation.id
                )
            )
            if existing is not None:
                ids.append(existing)
                continue
            if allocation.state != "ACTIVE":
                raise _DeletionControlError("allocation_state_changed")
            changed = db.execute(
                update(LegacyStorageAllocation)
                .where(
                    LegacyStorageAllocation.id == allocation.id,
                    LegacyStorageAllocation.state == "ACTIVE",
                )
                .values(state="DELETE_PENDING", updated_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed != 1:
                raise _DeletionControlError("allocation_state_changed")
            storage_bucket = allocation.storage_bucket
            storage_path = allocation.storage_path
        else:
            storage_bucket = upload_file.storage_bucket
            storage_path = upload_file.storage_path
        status = "pending"
        failure_code = None
        try:
            resolve_trusted_locator(
                storage_bucket,
                storage_path,
                record_id=upload_file.upload_id,
            )
        except ProviderError as error:
            status, failure_code = "failed", error.code

        intent = DeletionIntent(
            id=str(uuid.uuid4()),
            scope=scope,
            scope_id=scope_id,
            storage_bucket=storage_bucket,
            storage_path=storage_path,
            storage_allocation_id=upload_file.storage_allocation_id,
            status=status,
            failure_code=failure_code,
        )
        db.add(intent)
        ids.append(intent.id)
    return ids


def write_unreferenced_deletion_intents(
    db: Session, locators: Sequence[tuple[str, str, str]]
) -> list[str]:
    """Schedule deletion only for just-stored objects no persisted row points at.

    A locator a concurrently committed `UploadFile` already references is live
    backing bytes for someone else's record, so the loser of that race must
    leave it alone. `no_autoflush` keeps this request's own pending insert from
    being flushed into the answer and making it skip its own cleanup.
    """
    ids: list[str] = []
    for upload_id, storage_bucket, storage_path in locators:
        with db.no_autoflush:
            referenced = db.scalar(
                select(UploadFile.id)
                .where(
                    UploadFile.storage_bucket == storage_bucket,
                    UploadFile.storage_path == storage_path,
                )
                .limit(1)
            )
        if referenced is not None:
            continue
        ids += write_deletion_intents(
            db,
            scope="record",
            scope_id=upload_id,
            upload_files=[
                UploadFile(
                    upload_id=upload_id,
                    storage_bucket=storage_bucket,
                    storage_path=storage_path,
                )
            ],
        )
    return ids


def cleanup_stored_media_after_failure(
    session_factory, locators: Sequence[tuple[str, str, str]]
) -> None:
    """Best-effort: record and drive intents for just-stored bytes whose owning
    transaction failed, in a fresh transaction of its own.

    Swallows its own failures so the caller's original persistence exception is
    what propagates; logs the exception type only, never a locator or detail.
    """
    if not locators:
        return

    committed_ids: list[str] = []
    try:
        db = session_factory()
        try:
            intent_ids = write_unreferenced_deletion_intents(db, locators)
            db.commit()
            committed_ids = intent_ids
        finally:
            db.close()
    except Exception as error:
        logger.warning("Post-failure media cleanup could not be recorded: %s", type(error).__name__)
        return

    if not committed_ids:
        return
    try:
        drive_deletion_intents(session_factory, own_intent_ids=committed_ids)
    except Exception as error:
        logger.warning("Post-failure media cleanup could not be driven: %s", type(error).__name__)


def _delete_records(db: Session, upload_ids: Sequence[str]) -> None:
    chunk_ids = select(RecordChunk.id).where(RecordChunk.record_id.in_(upload_ids))
    db.query(RecordEmbedding).filter(RecordEmbedding.chunk_id.in_(chunk_ids)).delete(
        synchronize_session=False
    )
    db.query(RecordChunk).filter(RecordChunk.record_id.in_(upload_ids)).delete(
        synchronize_session=False
    )
    db.query(ProcessingJob).filter(ProcessingJob.record_id.in_(upload_ids)).delete(
        synchronize_session=False
    )
    db.query(UploadFile).filter(UploadFile.upload_id.in_(upload_ids)).delete(
        synchronize_session=False
    )
    db.query(Upload).filter(Upload.id.in_(upload_ids)).delete(synchronize_session=False)


def _lock_rooms(db: Session, room_ids: Sequence[str]) -> None:
    for room_id in sorted(set(room_ids)):
        _lock_room(db, room_id)


def delete_upload_record(db: Session, upload: Upload) -> list[str]:
    _lock_rooms(db, [upload.room_id])
    upload_files = db.scalars(
        select(UploadFile).where(UploadFile.upload_id == upload.id)
    ).all()
    intent_ids = write_deletion_intents(
        db, scope="record", scope_id=upload.id, upload_files=upload_files
    )
    _delete_records(db, [upload.id])
    return intent_ids


def delete_family_room_with_related_data(
    db: Session, room: FamilyRoom, *, _rooms_locked: bool = False
) -> list[str]:
    if not _rooms_locked:
        _lock_rooms(db, [room.id])
    room.is_active = False
    db.query(FamilyMember).filter(FamilyMember.room_id == room.id).update(
        {FamilyMember.is_active: False}, synchronize_session=False
    )
    for record in db.scalars(select(Record).where(Record.room_id == room.id)).all():
        db.add(RecordAudit(
            id=str(uuid.uuid4()), record_id=record.id, actor_user_id=room.owner_user_id,
            action="ROOM_DEACTIVATED", before_version=record.record_version,
            after_version=record.record_version + 1,
        ))
        record.authorization_epoch += 1
        record.record_version += 1
    return []


def delete_user_account_data(db: Session, user: User) -> list[str]:
    owned_room_ids = db.scalars(
        select(FamilyRoom.id).where(FamilyRoom.owner_user_id == user.id, FamilyRoom.is_active.is_(True))
    ).all()
    uploaded_room_ids = db.scalars(
        select(Upload.room_id).join(FamilyRoom, FamilyRoom.id == Upload.room_id)
        .where(Upload.uploader_user_id == user.id, FamilyRoom.is_active.is_(True))
    ).all()
    _lock_rooms(db, [*owned_room_ids, *uploaded_room_ids])

    for room in db.scalars(
        select(FamilyRoom)
        .where(FamilyRoom.owner_user_id == user.id, FamilyRoom.is_active.is_(True))
        .order_by(FamilyRoom.id)
    ).all():
        delete_family_room_with_related_data(
            db, room, _rooms_locked=True
        )
    db.query(FamilyMember).filter(FamilyMember.user_id == user.id).update(
        {FamilyMember.is_active: False}, synchronize_session=False
    )
    db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).delete(
        synchronize_session=False
    )
    user.google_sub = None
    user.username = None
    user.password_hash = None
    user.email = None
    user.profile_image = None
    user.age = None
    user.gender = None
    user.phone = None
    user.profile_chunk = None
    return []


def _delete_storage_object(storage_bucket: str, storage_path: str) -> str:
    """Delete one structurally trusted object; return ``deleted`` or ``absent``."""
    if storage_bucket == "local-media":
        root = LOCAL_MEDIA_ROOT.resolve()
        local_path = (root / storage_path).resolve()
        if Path(storage_path).name != storage_path or local_path.parent != root:
            raise _DeletionControlError("locator_untrusted")
        if not local_path.exists():
            return "absent"
        local_path.unlink()
        return "deleted"

    prefix, _, name = storage_path.partition("/")
    if (
        storage_bucket == settings.gcs_bucket_name
        and prefix == "uploads"
        and name
        and Path(name).name == name
    ):
        raise _DeletionControlError("historical_storage_unsupported")
    raise _DeletionControlError("locator_untrusted")


def drive_deletion_intents(
    session_factory, *, own_intent_ids: Sequence[str] = ()
) -> int:
    """Run storage I/O with no session open and persist its outcomes afterward."""
    own_ids = list(own_intent_ids)
    db = session_factory()
    try:
        own = (
            db.scalars(select(DeletionIntent).where(DeletionIntent.id.in_(own_ids))).all()
            if own_ids
            else []
        )
        backlog = db.scalars(
            select(DeletionIntent)
            .where(
                or_(
                    DeletionIntent.status == "pending",
                    (DeletionIntent.status == "failed")
                    & DeletionIntent.failure_code.not_in(
                        (
                            "locator_untrusted",
                            "historical_storage_unsupported",
                            "locator_referenced",
                        )
                    ),
                ),
                DeletionIntent.id.not_in(own_ids),
            )
            .order_by(DeletionIntent.created_at, DeletionIntent.id)
            .limit(INTENT_BATCH_LIMIT)
        ).all()
        intents = [*own, *backlog]
        stable = {
            intent.id
            for intent in intents
            if intent.status == "failed"
            and intent.failure_code
            in (
                "locator_untrusted",
                "historical_storage_unsupported",
                "locator_referenced",
            )
        }
        work = [
            (
                intent.id,
                intent.storage_bucket,
                intent.storage_path,
                intent.storage_allocation_id,
            )
            for intent in intents
            if intent.id not in stable
        ]
    finally:
        db.close()

    outcomes: dict[str, tuple[str, str | None]] = {
        intent.id: ("failed", intent.failure_code)
        for intent in intents
        if intent.id in stable
    }
    for intent_id, bucket, path, _allocation_id in work:
        try:
            outcomes[intent_id] = (_delete_storage_object(bucket, path), None)
        except _DeletionControlError as error:
            outcomes[intent_id] = ("failed", str(error))
        except Exception as error:
            logger.warning(
                "Failed to delete media (%s/%s): %s", bucket, path, type(error).__name__
            )
            outcomes[intent_id] = ("failed", "storage_delete_error")

    db = session_factory()
    try:
        # Acquire all surviving rooms before any allocation/intent writes; the
        # backlog may span rooms and its creation-time order is not a lock order.
        room_ids = db.scalars(
            select(LegacyStorageAllocation.room_id)
            .join(DeletionIntent, DeletionIntent.storage_allocation_id == LegacyStorageAllocation.id)
            .join(FamilyRoom, FamilyRoom.id == LegacyStorageAllocation.room_id)
            .where(DeletionIntent.id.in_(outcomes))
        ).all()
        for room_id in sorted(set(room_ids)):
            _lock_room(db, room_id, require_active=False)
        now = datetime.now(timezone.utc)
        for intent_id, (status, code) in sorted(outcomes.items()):
            intent = db.get(DeletionIntent, intent_id)
            if intent is None:
                continue
            if status in ("deleted", "absent") and intent.storage_allocation_id:
                released = db.execute(
                    update(LegacyStorageAllocation)
                    .where(
                        LegacyStorageAllocation.id == intent.storage_allocation_id,
                        LegacyStorageAllocation.state == "DELETE_PENDING",
                    )
                    .values(state="RELEASED", updated_at=now)
                    .execution_options(synchronize_session=False)
                ).rowcount
                if released != 1:
                    current = db.scalar(
                        select(LegacyStorageAllocation.state).where(
                            LegacyStorageAllocation.id == intent.storage_allocation_id
                        )
                    )
                    if current != "RELEASED":
                        status, code = "failed", "allocation_state_changed"
            db.execute(
                update(DeletionIntent)
                .where(DeletionIntent.id == intent_id)
                .values(status=status, failure_code=code, updated_at=now)
            )
        db.commit()
    finally:
        db.close()

    return sum(
        1
        for intent_id in own_ids
        if outcomes.get(intent_id, ("failed", None))[0] == "failed"
    )
