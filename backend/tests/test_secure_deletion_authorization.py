"""Secure deletion authorization through the real synthetic API boundary."""
import pytest
from sqlalchemy import text

from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import media_client, upload


@pytest.mark.parametrize("actor,allowed", [
    ("owner", False), ("uploader", True),
    ("member", False), ("outsider", False),
])
def test_delete_requires_record_owner_not_room_owner(media_client, actor, allowed):
    c = media_client
    owner_auth = c.auth
    member, _, _ = c.signup("member-user", "member@example.com")
    member_auth = {"Authorization": "Bearer " + member["access_token"]}
    joined = c.request("POST", "/families/join", headers=member_auth, json=dict(
        invite_code=c.room["invite_code"], related_to_user_id=c.session["user"]["user_id"],
        relation_to_related_user="child"))
    assert joined.status_code == 200, joined.text
    c.auth = member_auth
    row = upload(c)
    c.auth = owner_auth
    stranger, _, _ = c.signup("stranger-user", "stranger@example.com")
    stranger_auth = {"Authorization": "Bearer " + stranger["access_token"]}
    if actor == "member":
        joined = c.request("POST", "/families/join", headers=stranger_auth, json=dict(
            invite_code=c.room["invite_code"], related_to_user_id=c.session["user"]["user_id"],
            relation_to_related_user="child"))
        assert joined.status_code == 200, joined.text
    with c.engine.begin() as db:
        before = db.execute(text("SELECT deletion_epoch,authorization_epoch,revision FROM security_scopes "
                                 "WHERE domain='room' AND owner_id=:room"), {"room": c.room["room_id"]}).one()
    objects = dict(c.objects)
    auth = {"owner": owner_auth, "uploader": member_auth}.get(actor, stranger_auth)
    result = c.request("DELETE", "/uploads/" + row["upload_id"], headers=auth)
    assert (result.status_code == 200) is allowed, result.text
    with c.engine.connect() as db:
        tombstones = db.scalar(text("SELECT count(*) FROM security_tombstones WHERE target_id=:id"),
                               {"id": row["upload_id"]})
        inventory = db.scalar(text("SELECT count(*) FROM secure_deletion_inventory WHERE target_id=:id"),
                              {"id": row["upload_id"]})
        after = db.execute(text("SELECT deletion_epoch,authorization_epoch,revision FROM security_scopes "
                                "WHERE domain='room' AND owner_id=:room"), {"room": c.room["room_id"]}).one()
        assert tombstones == inventory == 0
        state = db.scalar(text("SELECT lifecycle_state FROM records WHERE id=:id"), {"id": row["upload_id"]})
        assert state == ("SOFT_DELETED" if allowed else "ACTIVE")
        assert tuple(after) == (before[0], before[1] + int(allowed), before[2] + int(allowed))
    assert c.objects == objects

def test_null_uploader_fails_closed_for_ordinary_member(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4
    from sqlalchemy import create_engine
    from app.services import secure_catalogue as cat
    from app.services.secure_deletion import tombstone_record

    principal, owner, room, record = (uuid4() for _ in range(4))
    # Older/imported provenance may be NULL even though today's baseline forbids it.
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        for sql in (
            "CREATE TABLE uploads (id TEXT,room_id TEXT,uploader_user_id TEXT)",
            "CREATE TABLE family_rooms (id TEXT,owner_user_id TEXT)",
            "CREATE TABLE family_members (room_id TEXT,user_id TEXT)",
            "CREATE TABLE security_scopes (domain TEXT,owner_id TEXT,state TEXT,deletion_epoch INT,authorization_epoch INT,revision INT)",
        ):
            db.execute(text(sql))
        db.execute(text("INSERT INTO uploads VALUES (:record,:room,NULL)"), dict(record=str(record), room=str(room)))
        db.execute(text("INSERT INTO family_rooms VALUES (:room,:owner)"), dict(room=str(room), owner=str(owner)))
        db.execute(text("INSERT INTO family_members VALUES (:room,:principal)"), dict(room=str(room), principal=str(principal)))
        db.execute(text("INSERT INTO security_scopes VALUES ('room',:room,'ACTIVE',0,0,1)"), dict(room=str(room)))
        monkeypatch.setattr(cat, "snapshot", lambda *a, **k: SimpleNamespace(
            principal_id=principal, scopes=(("room", room, 0, 0, 1),)))
        with pytest.raises(cat.SecureStateChanged):
            tombstone_record(db, principal_id=principal, record_id=record)
        assert tuple(db.execute(text("SELECT deletion_epoch,authorization_epoch,revision FROM security_scopes")).one()) == (0, 0, 1)


def test_failed_inventory_rolls_back_authorized_tombstone(media_client, monkeypatch):
    from app.services import secure_catalogue as cat

    c = media_client
    row = upload(c)
    with c.engine.connect() as db:
        before = db.execute(text("SELECT deletion_epoch,authorization_epoch,revision FROM security_scopes "
                                 "WHERE domain='room' AND owner_id=:room"), {"room": c.room["room_id"]}).one()
    original = cat._cas

    def fail_inventory(db, sql, **params):
        if sql.startswith("INSERT INTO secure_deletion_inventory"):
            raise cat.SecureStateChanged()
        return original(db, sql, **params)

    monkeypatch.setattr(cat, "_cas", fail_inventory)
    from app.services.secure_deletion import tombstone_record
    with pytest.raises(cat.SecureStateChanged), c.engine.begin() as db:
        tombstone_record(db, principal_id=c.session["user"]["user_id"], record_id=row["upload_id"])
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM security_tombstones")) == 0
        assert db.scalar(text("SELECT count(*) FROM secure_deletion_inventory")) == 0
        after = db.execute(text("SELECT deletion_epoch,authorization_epoch,revision FROM security_scopes "
                                "WHERE domain='room' AND owner_id=:room"), {"room": c.room["room_id"]}).one()
        assert after == before
