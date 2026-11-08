"""Synthetic stalled-writer and pending-cleanup reproductions."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from sqlalchemy import select
from app.api.routes import uploads
from app.models import LegacyStorageAllocation, DeletionIntent
from app.services import storage_quota, account_cleanup
from test_storage_quota_service import quota_case, _reserve, ROOM_ID, UPLOAD_IDS
from test_upload_immutable_publication import case, post


def test_stale_writer_cannot_publish_after_expiry_cleanup(quota_case, monkeypatch):
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", quota_case.root)
    monkeypatch.setattr(uploads, "SessionLocal", quota_case.sessions)
    old = datetime.now(timezone.utc) - storage_quota.RESERVATION_TTL - timedelta(seconds=1)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 3, now=old)
    ids = storage_quota.recover_expired(quota_case.sessions)
    account_cleanup.drive_deletion_intents(quota_case.sessions, own_intent_ids=ids)
    assert storage_quota.recover_expired(quota_case.sessions) == []
    try:
        uploads.store_media_file(b"123", UPLOAD_IDS[0]+".png", "image/png", attempt_id=reservation.id)
    except storage_quota.StorageAllocationStateChanged:
        pass
    storage_quota.abandon(quota_case.sessions, reservation)
    with quota_case.sessions() as db:
        charged = storage_quota._room_usage(db, ROOM_ID, 10)
        pending = db.scalar(select(DeletionIntent.id).where(DeletionIntent.storage_allocation_id == reservation.id, DeletionIntent.status.in_(("pending", "failed"))))
        assert not (quota_case.root / reservation.storage_path).exists() or charged > 0 or pending


def test_every_upload_retries_existing_pending_cleanup(case, monkeypatch):
    client, tracker, root = case
    from app.core.config import settings
    from test_processing_api import RECORD_ID
    payload = uploads.ONE_PIXEL_PNG
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", len(payload)*2+20)
    assert post(client, payload).status_code == 200
    real_delete = account_cleanup._delete_storage_object
    monkeypatch.setattr(account_cleanup, "_delete_storage_object", MagicMock(side_effect=OSError("busy")))
    assert post(client, payload).status_code == 200
    with tracker.session_maker() as db:
        assert db.scalar(select(LegacyStorageAllocation.id).where(LegacyStorageAllocation.state == "DELETE_PENDING"))
    monkeypatch.setattr(account_cleanup, "_delete_storage_object", real_delete)
    assert post(client, payload).status_code == 200


def test_expired_writer_rejected_even_before_recovery(quota_case, monkeypatch):
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", quota_case.root)
    monkeypatch.setattr(uploads, "SessionLocal", quota_case.sessions)
    old = datetime.now(timezone.utc) - storage_quota.RESERVATION_TTL - timedelta(seconds=1)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 3, now=old)
    with pytest.raises(storage_quota.StorageAllocationStateChanged):
        uploads.store_media_file(b"123", UPLOAD_IDS[0]+".png", "image/png", attempt_id=reservation.id)
    assert not (quota_case.root / reservation.storage_path).exists()
    ids = storage_quota.abandon(quota_case.sessions, reservation)
    account_cleanup.drive_deletion_intents(quota_case.sessions, own_intent_ids=ids)
    with quota_case.sessions() as db:
        assert storage_quota._room_usage(db, ROOM_ID, 10) == 0


def test_recovery_skips_live_writer_then_cleans(quota_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", quota_case.root)
    monkeypatch.setattr(uploads, "SessionLocal", quota_case.sessions)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 3)
    writing, finish = Event(), Event()
    real_store = uploads.store_media_file_locally
    def stalled(*args):
        writing.set()
        assert finish.wait(5)
        return real_store(*args)
    monkeypatch.setattr(uploads, "store_media_file_locally", stalled)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(uploads.store_media_file, b"123", UPLOAD_IDS[0]+".png", "image/png", attempt_id=reservation.id)
        assert writing.wait(5)
        recovery = pool.submit(storage_quota.recover_expired, quota_case.sessions, now=datetime.now(timezone.utc)+storage_quota.RESERVATION_TTL)
        try:
            assert recovery.result(timeout=5) == []
        finally:
            finish.set()
        writer.result(timeout=5)
    ids = storage_quota.recover_expired(quota_case.sessions, now=datetime.now(timezone.utc)+storage_quota.RESERVATION_TTL)
    account_cleanup.drive_deletion_intents(quota_case.sessions, own_intent_ids=ids)
    assert not (quota_case.root / reservation.storage_path).exists()
    assert storage_quota.recover_expired(quota_case.sessions) == []
    with quota_case.sessions() as db:
        assert storage_quota._room_usage(db, ROOM_ID, 10) == 0


def test_retry_failure_stays_charged_and_bounded(case, monkeypatch):
    client, tracker, _ = case
    from app.core.config import settings
    payload = uploads.ONE_PIXEL_PNG
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", len(payload)*2+20)
    assert post(client, payload).status_code == 200
    failing = MagicMock(side_effect=OSError("busy"))
    monkeypatch.setattr(account_cleanup, "_delete_storage_object", failing)
    assert post(client, payload).status_code == 200
    failing.reset_mock()
    assert post(client, payload).status_code == 413
    assert 1 <= failing.call_count <= account_cleanup.INTENT_BATCH_LIMIT
    with tracker.session_maker() as db:
        assert db.scalar(select(LegacyStorageAllocation.id).where(LegacyStorageAllocation.state == "DELETE_PENDING"))


def test_publication_locks_room_before_record(case, monkeypatch):
    from app.services import retrieval_indexing
    client, _, _ = case
    order = []
    real_lock, real_record = storage_quota._lock_room, retrieval_indexing.serialize_record
    def lock(db, room_id):
        order.append("room")
        return real_lock(db, room_id)
    def record(db, record_id):
        assert order and order[-1] == "room"
        order.append("record")
        return real_record(db, record_id)
    monkeypatch.setattr(storage_quota, "_lock_room", lock)
    monkeypatch.setattr(retrieval_indexing, "serialize_record", record)
    assert post(client, uploads.ONE_PIXEL_PNG).status_code == 200
    assert "record" in order



def test_lost_db_transaction_cannot_release_live_writer(quota_case, monkeypatch):
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", quota_case.root)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 3)
    captured = []
    def sessions():
        db = quota_case.sessions()
        captured.append(db)
        return db
    def resumed_writer():
        # SQL is closed before I/O; connection loss cannot release OS ownership.
        assert not captured[0].in_transaction()
        captured[0].invalidate()
        ids = storage_quota.recover_expired(
            quota_case.sessions, now=datetime.now(timezone.utc)+storage_quota.RESERVATION_TTL,
        )
        assert ids == []
        account_cleanup.drive_deletion_intents(quota_case.sessions, own_intent_ids=ids)
        return uploads.store_media_file_locally(b"123", reservation.storage_path)
    storage_quota.publish_local(sessions, allocation_id=reservation.id,
        storage_path=reservation.storage_path, byte_size=3, writer=resumed_writer)
    with quota_case.sessions() as db:
        assert storage_quota._room_usage(db, ROOM_ID, 10) == 3
    ids = storage_quota.abandon(quota_case.sessions, reservation)
    assert ids
    account_cleanup.drive_deletion_intents(quota_case.sessions, own_intent_ids=ids)
    assert not (quota_case.root / reservation.storage_path).exists()
    with quota_case.sessions() as db:
        assert storage_quota._room_usage(db, ROOM_ID, 10) == 0


def test_publication_lock_inode_is_persistent_and_outside_media(quota_case, monkeypatch):
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", quota_case.root)
    lock_path = quota_case.root.with_name("." + quota_case.root.name + ".publication.lock")
    with storage_quota._local_publication_lock():
        inode = lock_path.stat().st_ino
        with storage_quota._local_publication_lock(blocking=False) as acquired:
            assert not acquired
    with storage_quota._local_publication_lock():
        assert lock_path.stat().st_ino == inode
    assert lock_path.exists()
    assert lock_path.parent != quota_case.root


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX no-follow boundary")
def test_publication_lock_rejects_symlink(quota_case, monkeypatch):
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", quota_case.root)
    lock_path = quota_case.root.with_name("." + quota_case.root.name + ".publication.lock")
    target = quota_case.root / "do-not-touch"
    target.write_bytes(b"original")
    lock_path.symlink_to(target)
    with pytest.raises(OSError):
        with storage_quota._local_publication_lock():
            pytest.fail("symlink lock admitted")
    assert target.read_bytes() == b"original"



def test_room_deleted_during_local_write_retains_cleanup_responsibility(quota_case, monkeypatch):
    from app.models.family import FamilyRoom
    monkeypatch.setattr(uploads, "LOCAL_MEDIA_ROOT", quota_case.root)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 3)
    def writer():
        with quota_case.sessions() as db:
            account_cleanup.delete_family_room_with_related_data(db, db.get(FamilyRoom, ROOM_ID))
            db.commit()
        return uploads.store_media_file_locally(b"123", reservation.storage_path)
    storage_quota.publish_local(quota_case.sessions, allocation_id=reservation.id,
        storage_path=reservation.storage_path, byte_size=3, writer=writer)
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, reservation.id).state == "RESERVED"
        assert storage_quota._room_usage(db, ROOM_ID, 10) == 3
    ids = storage_quota.abandon(quota_case.sessions, reservation)
    assert ids
    account_cleanup.drive_deletion_intents(quota_case.sessions, own_intent_ids=ids)
    assert not (quota_case.root / reservation.storage_path).exists()
