"""AL-RT-03 room quota matrix on disposable local databases and files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import Base
from app.models import (
    DeletionIntent,
    FamilyRoom,
    LegacyStorageAllocation,
    Upload,
    UploadFile,
    User,
)
from app.services import account_cleanup, storage_quota
from app.services.account_cleanup import delete_upload_record, drive_deletion_intents
from app.services.storage_quota import (
    RESERVATION_TTL,
    RoomStorageQuotaExceeded,
    StorageAllocationStateChanged,
    _lock_room,
    _room_usage,
    abandon,
    activate,
    recover_expired,
    reserve,
    source_is_current_active,
)
from conftest import fk_enforced_engine

ROOM_ID = "quota-room"
USER_ID = "quota-user"
UPLOAD_IDS = tuple(f"quota-upload-{index}" for index in range(1, 5))


@pytest.fixture
def quota_case(tmp_path, monkeypatch):
    engine = fk_enforced_engine(
        f"sqlite:///{tmp_path / 'quota.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(User(
            id=USER_ID, username="quota_user", password_hash="x",
            email="quota@example.test", name="Quota User",
        ))
        db.flush()
        db.add(FamilyRoom(
            id=ROOM_ID, name="Quota", invite_code="QUOTA001",
            owner_user_id=USER_ID,
        ))
        db.flush()
        for upload_id in UPLOAD_IDS:
            db.add(Upload(
                id=upload_id, room_id=ROOM_ID, uploader_user_id=USER_ID,
                type="image", title=upload_id, status="uploaded",
            ))
        db.commit()
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", 10)
    monkeypatch.setattr(account_cleanup, "LOCAL_MEDIA_ROOT", tmp_path)
    try:
        yield SimpleNamespace(
            engine=engine, sessions=sessions, root=tmp_path,
        )
    finally:
        engine.dispose()


def _reserve(case, upload_id: str, size: int, *, now=None):
    with case.sessions() as db:
        return reserve(
            db, room_id=ROOM_ID, upload_id=upload_id,
            byte_size=size, suffix=".png", now=now,
        )


def _publish(case, upload_id: str, data: bytes):
    reservation = _reserve(case, upload_id, len(data))
    (case.root / reservation.storage_path).write_bytes(data)
    with case.sessions() as db:
        upload_file = UploadFile(
            id=str(uuid4()), upload_id=upload_id,
            storage_bucket=reservation.storage_bucket,
            storage_path=reservation.storage_path,
            storage_allocation_id=reservation.id,
            mime_type="image/png", file_size=len(data), encrypted=False,
            created_at=datetime.now(timezone.utc),
        )
        intent_ids = activate(db, reservation, upload_file=upload_file)
        db.commit()
        return reservation, upload_file.id, intent_ids


@pytest.mark.parametrize(
    "size,allowed",
    [(9, True), (10, True), (11, False)],
    ids=("under", "exact", "plus-one"),
)
def test_quota_boundaries(quota_case, size, allowed):
    if allowed:
        assert _reserve(quota_case, UPLOAD_IDS[0], size).byte_size == size
    else:
        with pytest.raises(RoomStorageQuotaExceeded):
            _reserve(quota_case, UPLOAD_IDS[0], size)


def test_repeated_reservations_cannot_over_allocate(quota_case):
    _reserve(quota_case, UPLOAD_IDS[0], 6)
    with pytest.raises(RoomStorageQuotaExceeded):
        _reserve(quota_case, UPLOAD_IDS[1], 5)


def test_original_twenty_mib_reproduction_is_bounded(quota_case, monkeypatch):
    twenty_mib = 20 * 1024 * 1024
    monkeypatch.setattr(
        settings, "legacy_room_storage_quota_bytes", twenty_mib
    )
    assert _reserve(quota_case, UPLOAD_IDS[0], twenty_mib).byte_size == twenty_mib
    with pytest.raises(RoomStorageQuotaExceeded):
        _reserve(quota_case, UPLOAD_IDS[1], twenty_mib)


def test_file_backed_sqlite_serializes_concurrent_near_limit_reservations(quota_case):
    barrier = Barrier(2)

    def attempt(upload_id):
        barrier.wait()
        try:
            return _reserve(quota_case, upload_id, 6).id
        except RoomStorageQuotaExceeded:
            return "quota"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, UPLOAD_IDS[:2]))

    assert sorted("quota" if value == "quota" else "reserved" for value in results) == [
        "quota", "reserved",
    ]
    with quota_case.sessions() as db:
        assert _room_usage(db, ROOM_ID, 10) == 6


def test_failed_publication_releases_absent_reservation(quota_case):
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 6)
    intent_ids = abandon(quota_case.sessions, reservation)
    assert drive_deletion_intents(
        quota_case.sessions, own_intent_ids=intent_ids
    ) == 0
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, reservation.id).state == "RELEASED"
        assert db.get(DeletionIntent, intent_ids[0]).status == "absent"


def test_record_delete_releases_only_after_physical_delete(quota_case):
    reservation, _, _ = _publish(quota_case, UPLOAD_IDS[0], b"123456")
    with quota_case.sessions() as db:
        intent_ids = delete_upload_record(db, db.get(Upload, UPLOAD_IDS[0]))
        db.commit()
    assert (quota_case.root / reservation.storage_path).exists()
    assert drive_deletion_intents(
        quota_case.sessions, own_intent_ids=intent_ids
    ) == 0
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, reservation.id).state == "RELEASED"
        assert db.get(DeletionIntent, intent_ids[0]).status == "deleted"
    assert not (quota_case.root / reservation.storage_path).exists()


def test_replacement_keeps_new_current_and_releases_old(quota_case):
    old, old_file_id, _ = _publish(quota_case, UPLOAD_IDS[0], b"1234")
    new, new_file_id, intent_ids = _publish(quota_case, UPLOAD_IDS[0], b"5678")
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, old.id).state == "DELETE_PENDING"
        assert db.get(LegacyStorageAllocation, new.id).state == "ACTIVE"
        assert not source_is_current_active(db, db.get(UploadFile, old_file_id))
        assert source_is_current_active(db, db.get(UploadFile, new_file_id))
    assert (quota_case.root / old.storage_path).exists()
    assert (quota_case.root / new.storage_path).exists()
    drive_deletion_intents(quota_case.sessions, own_intent_ids=intent_ids)
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, old.id).state == "RELEASED"
        assert db.get(LegacyStorageAllocation, new.id).state == "ACTIVE"
        assert _room_usage(db, ROOM_ID, 10) == 4
    assert not (quota_case.root / old.storage_path).exists()
    assert (quota_case.root / new.storage_path).read_bytes() == b"5678"


def test_post_publication_auth_rejection_cleans_and_releases(quota_case):
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 6)
    (quota_case.root / reservation.storage_path).write_bytes(b"123456")
    intent_ids = abandon(quota_case.sessions, reservation)
    drive_deletion_intents(quota_case.sessions, own_intent_ids=intent_ids)
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, reservation.id).state == "RELEASED"
        assert _room_usage(db, ROOM_ID, 10) == 0
    assert not (quota_case.root / reservation.storage_path).exists()


def test_restart_recovery_cas_releases_expired_reservation(quota_case):
    past = datetime.now(timezone.utc) - RESERVATION_TTL - timedelta(seconds=1)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 6, now=past)
    (quota_case.root / reservation.storage_path).write_bytes(b"123456")
    intent_ids = recover_expired(quota_case.sessions)
    drive_deletion_intents(quota_case.sessions, own_intent_ids=intent_ids)
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, reservation.id).state == "RELEASED"


def test_restart_recovery_adopts_current_unlinked_publication_without_double_charge(
    quota_case,
):
    past = datetime.now(timezone.utc) - RESERVATION_TTL - timedelta(seconds=1)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 6, now=past)
    path = quota_case.root / reservation.storage_path
    path.write_bytes(b"123456")
    with quota_case.sessions() as db:
        winner = UploadFile(
            id="winner", upload_id=UPLOAD_IDS[0], storage_bucket="local-media",
            storage_path=reservation.storage_path, mime_type="image/png",
            file_size=6, encrypted=False,
        )
        db.add(winner)
        db.commit()
        assert _room_usage(db, ROOM_ID, 20) == 12

    assert recover_expired(quota_case.sessions) == []
    with quota_case.sessions() as db:
        allocation = db.get(LegacyStorageAllocation, reservation.id)
        winner = db.get(UploadFile, "winner")
        assert allocation.state == "ACTIVE"
        assert winner.storage_allocation_id == reservation.id
        assert source_is_current_active(db, winner)
        assert _room_usage(db, ROOM_ID, 20) == 6
    assert path.read_bytes() == b"123456"


def test_restart_recovery_releases_loser_owned_by_current_active_allocation(
    quota_case, monkeypatch,
):
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", 20)
    winner, winner_file_id, _ = _publish(
        quota_case, UPLOAD_IDS[0], b"123456"
    )
    past = datetime.now(timezone.utc) - RESERVATION_TTL - timedelta(seconds=1)
    loser = _reserve(quota_case, UPLOAD_IDS[1], 6, now=past)
    loser_path = quota_case.root / loser.storage_path
    loser_path.write_bytes(b"123456")

    class CollisionSession:
        def __init__(self):
            self.real = quota_case.sessions()

        def __getattr__(self, name):
            return getattr(self.real, name)

        def get(self, model, key):
            if model is LegacyStorageAllocation and key == winner.id:
                return SimpleNamespace(
                    id=winner.id,
                    state="ACTIVE",
                    room_id=loser.room_id,
                    storage_bucket=loser.storage_bucket,
                    storage_path=loser.storage_path,
                    byte_size=loser.byte_size,
                )
            return self.real.get(model, key)

    monkeypatch.setattr(
        storage_quota,
        "_referencing_file",
        lambda db, _reservation: db.get(UploadFile, winner_file_id),
    )
    assert recover_expired(CollisionSession) == []

    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, loser.id).state == "RELEASED"
        assert db.get(LegacyStorageAllocation, winner.id).state == "ACTIVE"
        assert _room_usage(db, ROOM_ID, 20) == 6
        assert db.scalars(select(DeletionIntent)).all() == []
    assert loser_path.read_bytes() == b"123456"


def test_ambiguous_referenced_reservation_stays_charged_and_visible(quota_case):
    past = datetime.now(timezone.utc) - RESERVATION_TTL - timedelta(seconds=1)
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 6, now=past)
    path = quota_case.root / reservation.storage_path
    path.write_bytes(b"123456")
    with quota_case.sessions() as db:
        db.add(UploadFile(
            id="foreign-winner", upload_id=UPLOAD_IDS[1],
            storage_bucket="local-media", storage_path=reservation.storage_path,
            mime_type="image/png", file_size=6, encrypted=False,
        ))
        db.commit()

    intent_ids = recover_expired(quota_case.sessions)
    assert drive_deletion_intents(
        quota_case.sessions, own_intent_ids=intent_ids
    ) == 1
    with quota_case.sessions() as db:
        allocation = db.get(LegacyStorageAllocation, reservation.id)
        intent = db.get(DeletionIntent, intent_ids[0])
        assert allocation.state == "DELETE_PENDING"
        assert (intent.status, intent.failure_code) == (
            "failed", "locator_referenced",
        )
    assert path.read_bytes() == b"123456"


def test_postgresql_room_lock_compiles_select_for_update():
    class Bind:
        dialect = SimpleNamespace(name="postgresql")

    class Capture:
        statement = None

        def get_bind(self):
            return Bind()

        def scalar(self, statement):
            self.statement = statement
            return ROOM_ID

    db = Capture()
    _lock_room(db, ROOM_ID)
    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


@pytest.mark.parametrize("scope", ["record", "room"])
def test_deletion_locks_room_before_final_file_snapshot(
    quota_case, monkeypatch, scope,
):
    _publish(quota_case, UPLOAD_IDS[0], b"123")
    events = []
    real_write = account_cleanup.write_deletion_intents

    monkeypatch.setattr(
        account_cleanup,
        "_lock_room",
        lambda _db, room_id: events.append(("lock", room_id)),
        raising=False,
    )

    def observed_write(db, **kwargs):
        events.append(("snapshot", kwargs["scope"]))
        return real_write(db, **kwargs)

    monkeypatch.setattr(account_cleanup, "write_deletion_intents", observed_write)
    with quota_case.sessions() as db:
        if scope == "record":
            account_cleanup.delete_upload_record(
                db, db.get(Upload, UPLOAD_IDS[0])
            )
        else:
            intent_ids = account_cleanup.delete_family_room_with_related_data(
                db, db.get(FamilyRoom, ROOM_ID)
            )
            db.flush()
            assert intent_ids == []
            assert not db.get(FamilyRoom, ROOM_ID).is_active
            assert db.get(Upload, UPLOAD_IDS[0]) is not None
            assert db.scalar(select(UploadFile.id).where(UploadFile.upload_id == UPLOAD_IDS[0])) is not None
            assert db.scalar(select(DeletionIntent.id)) is None
        db.rollback()

    assert events[0] == ("lock", ROOM_ID)
    if scope == "record":
        assert events[1] == ("snapshot", scope)
    else:
        assert events == [("lock", ROOM_ID)]


def test_account_deletion_locks_all_rooms_once_in_sorted_order(
    quota_case, monkeypatch,
):
    with quota_case.sessions() as db:
        db.add(User(
            id="other-owner", username="other_owner", password_hash="x",
            email="other@example.test", name="Other",
        ))
        db.flush()
        db.add_all([
            FamilyRoom(
                id="z-owned-room", name="Owned", invite_code="OWNED001",
                owner_user_id=USER_ID,
            ),
            FamilyRoom(
                id="a-foreign-room", name="Foreign", invite_code="FOREIGN1",
                owner_user_id="other-owner",
            ),
        ])
        db.flush()
        db.add(Upload(
            id="foreign-room-upload", room_id="a-foreign-room",
            uploader_user_id=USER_ID, type="image", title="foreign",
            status="uploaded",
        ))
        db.commit()

    locked = []
    monkeypatch.setattr(
        account_cleanup,
        "_lock_room",
        lambda _db, room_id: locked.append(room_id),
        raising=False,
    )
    with quota_case.sessions() as db:
        account_cleanup.delete_user_account_data(db, db.get(User, USER_ID))
        db.rollback()

    assert locked == ["a-foreign-room", ROOM_ID, "z-owned-room"]


def test_inflight_reservation_rejects_after_record_delete_and_recovers(
    quota_case,
):
    reservation = _reserve(quota_case, UPLOAD_IDS[0], 3)
    path = quota_case.root / reservation.storage_path
    path.write_bytes(b"123")
    with quota_case.sessions() as db:
        account_cleanup.delete_upload_record(db, db.get(Upload, UPLOAD_IDS[0]))
        db.commit()

    with quota_case.sessions() as db:
        upload_file = UploadFile(
            id="late-publication", upload_id=UPLOAD_IDS[0],
            storage_bucket=reservation.storage_bucket,
            storage_path=reservation.storage_path,
            mime_type="image/png", file_size=3, encrypted=False,
        )
        with pytest.raises(StorageAllocationStateChanged):
            activate(db, reservation, upload_file=upload_file)
        db.rollback()

    intent_ids = abandon(quota_case.sessions, reservation)
    drive_deletion_intents(quota_case.sessions, own_intent_ids=intent_ids)
    with quota_case.sessions() as db:
        assert db.get(LegacyStorageAllocation, reservation.id).state == "RELEASED"
        assert _room_usage(db, ROOM_ID, 10) == 0
    assert not path.exists()


def test_later_activation_normalizes_current_order_after_room_lock(quota_case):
    old, old_file_id, _ = _publish(quota_case, UPLOAD_IDS[0], b"123")
    replacement = _reserve(quota_case, UPLOAD_IDS[0], 3)
    with quota_case.sessions() as db:
        old_file = db.get(UploadFile, old_file_id)
        new_file = UploadFile(
            id="later-activation", upload_id=UPLOAD_IDS[0],
            storage_bucket=replacement.storage_bucket,
            storage_path=replacement.storage_path,
            mime_type="image/png", file_size=3, encrypted=False,
            created_at=old_file.created_at - timedelta(days=1),
        )
        activate(db, replacement, upload_file=new_file)
        db.commit()

    with quota_case.sessions() as db:
        old_file = db.get(UploadFile, old_file_id)
        new_file = db.get(UploadFile, "later-activation")
        assert db.get(LegacyStorageAllocation, old.id).state == "DELETE_PENDING"
        assert db.get(LegacyStorageAllocation, replacement.id).state == "ACTIVE"
        assert new_file.created_at > old_file.created_at
        assert source_is_current_active(db, new_file)
        assert not source_is_current_active(db, old_file)
