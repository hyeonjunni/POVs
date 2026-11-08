from collections.abc import Iterator

import pytest
from app.models.record import Record
from app.services.record_policy import (
    allowed_record_ids,
    record_capabilities,
    require_record_access,
)
from conftest import fk_enforced_engine
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection


@pytest.fixture
def policy_db() -> Iterator[Connection]:
    engine = fk_enforced_engine()
    Record.metadata.create_all(engine)
    with engine.begin() as db:
        for uid in ("owner", "subject", "admin", "other"):
            db.execute(text("INSERT INTO users(id,name,email) VALUES (:id,:id,:id)"), {"id": uid})
        db.execute(text("INSERT INTO family_rooms(id,name,invite_code,owner_user_id) VALUES ('room','room','code','owner')"))
        for uid in ("owner", "subject", "admin", "other"):
            db.execute(text("INSERT INTO family_members(id,room_id,user_id,role) VALUES (:id,'room',:id,:role)"), {"id": uid, "role": "admin" if uid == "admin" else "member"})
        for rid, visibility in (("private", "PRIVATE"), ("family", "FAMILY"), ("custom", "CUSTOM")):
            db.execute(text("INSERT INTO uploads(id,room_id,uploader_user_id,type,status) VALUES (:id,'room','subject','text','uploaded')"), {"id": rid})
            db.execute(text("INSERT INTO records(id,room_id,owner_member_id,uploader_member_id,visibility,review_status) VALUES (:id,'room','owner','subject',:visibility,'APPROVED')"), {"id": rid, "visibility": visibility})
        yield db
    engine.dispose()


@pytest.mark.parametrize("viewer", ["subject", "admin", "other"])
def test_private_denial_matches_missing_record(policy_db: Connection, viewer: str) -> None:
    failures = []
    for rid in ("private", "absent"):
        with pytest.raises(HTTPException) as caught:
            require_record_access(policy_db, viewer, rid)
        failures.append((caught.value.status_code, caught.value.detail))
    assert failures == [(404, {"code": "RESOURCE_NOT_AVAILABLE"})] * 2


def test_custom_requires_explicit_grant(policy_db: Connection) -> None:
    policy_db.execute(text("INSERT INTO record_grants VALUES ('custom','subject')"))
    assert allowed_record_ids(policy_db, "subject", "room", purpose="rag") == ["family", "custom"]
    assert allowed_record_ids(policy_db, "admin", "room", purpose="rag") == ["family"]


def test_multiple_restrictions_intersect(policy_db: Connection) -> None:
    policy_db.execute(text("INSERT INTO record_restrictions(id,record_id,member_id,active) VALUES ('one','family','subject',true),('two','family','other',true)"))
    policy_db.execute(text("INSERT INTO record_restriction_grants VALUES ('one','subject'),('one','admin'),('two','subject')"))
    assert allowed_record_ids(policy_db, "subject", "room") == ["family"]
    assert allowed_record_ids(policy_db, "admin", "room") == []
    assert "family" in allowed_record_ids(policy_db, "owner", "room")


def test_subject_capability_requires_approved_link(policy_db: Connection) -> None:
    policy_db.execute(text("INSERT INTO record_people VALUES ('family','subject','SUBJECT',false)"))
    assert not record_capabilities(policy_db, "subject", "family").can_restrict
    policy_db.execute(text("UPDATE record_people SET approved=true"))
    assert record_capabilities(policy_db, "subject", "family").can_restrict
    assert not record_capabilities(policy_db, "subject", "family").can_approve_purge


@pytest.mark.parametrize("state", ["RESTRICTED", "HIDDEN", "ARCHIVED", "DELETION_REQUESTED", "SOFT_DELETED", "PURGED"])
def test_non_active_records_never_enter_rag(policy_db: Connection, state: str) -> None:
    policy_db.execute(text("UPDATE records SET lifecycle_state=:state WHERE id='family'"), {"state": state})
    assert allowed_record_ids(policy_db, "admin", "room", purpose="rag") == []


def test_departed_owner_has_only_personal_management(policy_db: Connection) -> None:
    policy_db.execute(text("UPDATE family_members SET is_active=false WHERE id='owner'"))
    assert allowed_record_ids(policy_db, "owner", "room", purpose="rag") == []
    assert allowed_record_ids(policy_db, "owner", "room", purpose="manage") == ["private"]
    assert record_capabilities(policy_db, "owner", "private").can_soft_delete
    assert not record_capabilities(policy_db, "owner", "private").can_edit_content


def test_missing_policy_row_fails_closed(policy_db: Connection) -> None:
    policy_db.execute(text("DELETE FROM records WHERE id='family'"))
    with pytest.raises(HTTPException) as caught:
        require_record_access(policy_db, "owner", "family")
    assert caught.value.status_code == 404


def test_personal_and_family_search_scopes_do_not_mix(policy_db: Connection) -> None:
    assert allowed_record_ids(policy_db, "owner", "room", purpose="rag", scope="PERSONAL") == ["private"]
    assert set(allowed_record_ids(policy_db, "owner", "room", purpose="rag", scope="FAMILY")) == {"family", "custom"}


def test_pending_review_excluded_before_candidates(policy_db: Connection) -> None:
    policy_db.execute(text("UPDATE records SET review_status='PENDING_REVIEW' WHERE id='family'"))
    assert allowed_record_ids(policy_db, "admin", "room", purpose="rag") == []
    assert allowed_record_ids(policy_db, "admin", "room", purpose="read") == ["family"]


def test_deletion_request_does_not_expose_to_remaining_family(policy_db: Connection) -> None:
    policy_db.execute(text("UPDATE records SET lifecycle_state='DELETION_REQUESTED' WHERE id='family'"))
    policy_db.execute(text("INSERT INTO record_deletion_requests(id,record_id,requester_member_id,reason_digest) VALUES ('request','family','subject','digest')"))
    assert allowed_record_ids(policy_db, "admin", "room") == []
    assert allowed_record_ids(policy_db, "subject", "room") == ["family"]


@pytest.mark.parametrize("state", ["HIDDEN", "ARCHIVED"])
def test_historical_requester_cannot_read_owner_only_states(policy_db: Connection, state: str) -> None:
    policy_db.execute(text("UPDATE records SET lifecycle_state=:state WHERE id='family'"), {"state": state})
    policy_db.execute(text("INSERT INTO record_deletion_requests(id,record_id,requester_member_id,reason_digest) VALUES ('request','family','subject','digest')"))
    assert allowed_record_ids(policy_db, "subject", "room") == []


def test_closed_request_does_not_grant_deletion_request_access(policy_db: Connection) -> None:
    policy_db.execute(text("UPDATE records SET lifecycle_state='DELETION_REQUESTED' WHERE id='family'"))
    policy_db.execute(text("INSERT INTO record_deletion_requests(id,record_id,requester_member_id,reason_digest,status) VALUES ('request','family','subject','digest','CLOSED')"))
    assert allowed_record_ids(policy_db, "subject", "room") == []
