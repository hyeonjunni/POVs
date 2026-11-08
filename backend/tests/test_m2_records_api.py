from uuid import uuid4

import pytest
from sqlalchemy import text
from test_native_text_pipeline import indexed_native as _indexed_native
from test_native_text_pipeline import native_case as _native_case
from test_secure_catalogue import catalogue as _catalogue
from test_secure_identity import identity_client as _identity_client
from test_secure_media import media_client as _media_client

catalogue = _catalogue
identity_client = _identity_client
media_client = _media_client
native_case = _native_case
indexed_native = _indexed_native


def create(client, **fields):
    return client.request("POST", "/records", headers={**client.auth, "Idempotency-Key": str(uuid4())},
        json={"room_id": client.room["room_id"], "type": "text", "title": "Sensitive title", "description": "Private source", **fields})


def mutate(client, row, path, payload, **headers):
    return client.request("POST", f"/records/{row['record_id']}/{path}",
        headers={**client.auth, "If-Match": f'"{row["record_version"]}"', "Idempotency-Key": str(uuid4()), **headers}, json=payload)


def test_private_default_and_legacy_family_default(media_client):
    client = media_client
    canonical = create(client)
    assert canonical.status_code == 200, canonical.text
    assert canonical.json()["visibility"] == "PRIVATE"
    assert canonical.json()["review_status"] == "PENDING_REVIEW"
    assert canonical.headers["etag"] == '"1"'
    legacy = client.request("POST", "/uploads", headers=client.auth,
        json={"room_id": client.room["room_id"], "type": "text", "title": "Legacy"})
    assert legacy.status_code == 200, legacy.text
    read = client.request("GET", f"/records/{legacy.json()['upload_id']}", headers=client.auth)
    assert read.json()["visibility"] == "FAMILY"


def test_creation_idempotency_replays_same_record_and_rejects_changed_payload(media_client):
    client = media_client
    headers = {**client.auth, "Idempotency-Key": str(uuid4())}
    payload = {"room_id": client.room["room_id"], "type": "text", "title": "one"}
    first = client.request("POST", "/records", headers=headers, json=payload)
    second = client.request("POST", "/records", headers=headers, json=payload)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["record_id"] == second.json()["record_id"]
    changed = client.request("POST", "/records", headers=headers, json={**payload, "title": "two"})
    assert changed.status_code == 409


def test_archive_soft_delete_restore_requires_new_review(media_client):
    client = media_client
    row = create(client).json()
    deleted = mutate(client, row, "lifecycle-transitions", {"action": "SOFT_DELETE", "reason": "owner request"})
    assert deleted.status_code == 200, deleted.text
    assert "title" not in deleted.json()
    unavailable = client.request("GET", f"/records/{row['record_id']}", headers=client.auth)
    assert unavailable.status_code == 404
    restored = mutate(client, deleted.json(), "lifecycle-transitions", {"action": "RESTORE", "reason": "recover"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["lifecycle_state"] == "ARCHIVED"
    assert restored.json()["review_status"] == "PENDING_REVIEW"


def test_mutation_requires_if_match_and_rejects_stale_revision(media_client):
    client = media_client
    row = create(client).json()
    path = f"/records/{row['record_id']}/lifecycle-transitions"
    payload = {"action": "ARCHIVE", "reason": "store"}
    missing = client.request("POST", path, headers={**client.auth, "Idempotency-Key": str(uuid4())}, json=payload)
    assert missing.status_code == 428, missing.text
    result = mutate(client, row, "lifecycle-transitions", payload)
    assert result.status_code == 200, result.text
    stale = mutate(client, row, "lifecycle-transitions", payload)
    assert stale.status_code == 412, stale.text


def test_metadata_versions_do_not_change_original_source(media_client):
    client = media_client
    row = create(client).json()
    result = client.request("PATCH", f"/records/{row['record_id']}/metadata",
        headers={**client.auth, "If-Match": '"1"'}, json={"reason": "correct date", "changes": {"occurred_at": "2025-01-02"}})
    assert result.status_code == 200, result.text
    edited = result.json()
    assert edited["metadata_revision"] == row["metadata_revision"] + 1
    assert edited["source_revision"] == row["source_revision"]
    assert edited["metadata"]["occurred_at"] == "2025-01-02"
    with client.engine.connect() as db:
        audit = str(db.execute(text("SELECT * FROM record_audits")).all())
        revisions = str(db.execute(text("SELECT * FROM record_metadata_revisions")).all())
    assert "correct date" not in audit + revisions
    assert "2025-01-02" not in audit + revisions
    history = client.request("GET", f"/records/{row['record_id']}/metadata-revisions", headers=client.auth)
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["resulting_record_version"] == edited["record_version"]
    assert history.json()["items"][0]["detail"]["changes"] == {"occurred_at": "2025-01-02"}


def test_purge_is_blocked_before_thirty_days(media_client):
    client = media_client
    row = create(client).json()
    deleted = mutate(client, row, "lifecycle-transitions", {"action": "SOFT_DELETE", "reason": "delete"}).json()
    result = mutate(client, deleted, "purge-requests", {"reason": "purge", "confirmation": "PERMANENTLY_DELETE"})
    assert result.status_code == 409, result.text


def test_classification_metadata_preserves_media_child_data(media_client):
    client = media_client
    row = create(client, type="image").json()
    changes = {
        "source_record_id": "IC01", "event_ids": ["C01"],
        "record_kind": "SHARED", "authored_at": "2026-10-04",
        "occurred_at": "2009-05-17",
        "source_provenance": {"document_name": "final.docx", "source_section": "C01",
                              "synthetic": True, "classification_reason": "Shared family event"},
        "image_metadata": {"caption": "A family at the pond", "generation_prompt": "Reference prompt",
                           "transcription_gold": "Album date: 2009-05-17"},
    }

    result = client.request("PATCH", f"/records/{row['record_id']}/metadata",
        headers={**client.auth, "If-Match": '"1"'},
        json={"reason": "Classify Word source", "changes": changes})

    assert result.status_code == 200, result.text
    assert result.json()["metadata"] == changes
    assert result.json()["review_status"] == "PENDING_REVIEW"
    assert result.json()["source_revision"] == row["source_revision"]
    assert result.json()["title"] == row["title"]
    read = client.request("GET", f"/records/{row['record_id']}", headers=client.auth)
    assert read.json()["metadata"] == changes


def test_event_source_uses_native_text_publication(media_client):
    client = media_client
    row = create(client, title="Family walk", description="2009-05-17: Seoul Forest").json()
    result = client.request("PATCH", f"/records/{row['record_id']}/metadata",
        headers={**client.auth, "If-Match": '"1"'},
        json={"reason": "Separate event source", "changes": {"record_kind": "EVENT", "event_ids": ["C01"]}})
    assert result.status_code == 200, result.text

    published = client.request("POST", f"/uploads/{row['record_id']}/publish-text", headers=client.auth)

    assert published.status_code == 200, published.text
    assert published.json()["type"] == "text"
    read = client.request("GET", f"/records/{row['record_id']}", headers=client.auth)
    assert read.json()["metadata"]["record_kind"] == "EVENT"
    assert read.json()["metadata"]["event_ids"] == ["C01"]


def test_non_owner_cannot_rewrite_source_classification(media_client):
    client = media_client
    admin, _ = family_actor(client, "source-admin", "admin")
    row = create(client, visibility="FAMILY").json()

    result = client.request("PATCH", f"/records/{row['record_id']}/metadata",
        headers={**admin, "If-Match": '"1"'},
        json={"reason": "Rewrite authorship", "changes": {"record_kind": "PERSONAL"}})

    assert result.status_code == 403, result.text


def test_visibility_switch_is_owner_only_and_rejects_stale_versions(media_client):
    client = media_client
    viewer, _ = family_actor(client, "sharing-viewer", "admin")
    row = create(client).json()
    shared = mutate(client, row, "visibility-changes", {"reason": "Share", "visibility": "FAMILY"})
    assert shared.status_code == 200, shared.text
    assert client.request("GET", f"/records/{row['record_id']}", headers=viewer).status_code == 200

    denied = mutate(client, shared.json(), "visibility-changes",
                    {"reason": "Not mine", "visibility": "PRIVATE"}, **viewer)
    stale = mutate(client, row, "visibility-changes", {"reason": "Stale", "visibility": "PRIVATE"})
    private = mutate(client, shared.json(), "visibility-changes", {"reason": "Withdraw", "visibility": "PRIVATE"})

    assert denied.status_code == 403, denied.text
    assert stale.status_code == 412, stale.text
    assert private.status_code == 200, private.text
    assert client.request("GET", f"/records/{row['record_id']}", headers=viewer).status_code == 404


def test_private_switch_revokes_existing_image_content_url(media_client):
    from test_secure_media import content_path, upload

    client = media_client
    viewer, _ = family_actor(client, "image-viewer")
    row = create(client, type="image", visibility="FAMILY").json()
    binary = upload(client, {"upload_id": row["record_id"]})
    path = content_path(binary)
    assert client.request("GET", path, headers=viewer).status_code == 200
    current = client.request("GET", f"/records/{row['record_id']}", headers=client.auth).json()

    private = mutate(client, current, "visibility-changes", {"reason": "Withdraw", "visibility": "PRIVATE"})

    assert private.status_code == 200, private.text
    assert client.request("GET", path, headers=viewer).status_code == 404
    assert client.request("GET", path, headers=client.auth).status_code == 200


@pytest.mark.parametrize("lifecycle", ["ACTIVE", "SOFT_DELETED"])
def test_operator_quarantine_denies_owner_read_file_and_restore(media_client, monkeypatch, lifecycle):
    from uuid import UUID
    from app.core.config import settings
    from test_secure_media import content_path, upload

    client = media_client
    viewer, _ = family_actor(client, "quarantine-viewer")
    row = create(client, type="image", visibility="FAMILY").json()
    binary = upload(client, {"upload_id": row["record_id"]})
    rid = row["record_id"]
    current = client.request("GET", f"/records/{rid}", headers=client.auth).json()
    if lifecycle == "SOFT_DELETED":
        current = mutate(client, current, "lifecycle-transitions", {"action": "SOFT_DELETE", "reason": "Replace source"}).json()
    monkeypatch.setattr(settings, "quarantined_record_ids", [UUID(rid)])

    for actor in (client.auth, viewer):
        assert client.request("GET", f"/records/{rid}", headers=actor).status_code == 404
        assert client.request("GET", f"/records/{rid}/management", headers=actor).status_code == 404
        assert client.request("GET", content_path(binary), headers=actor).status_code == 404
        listed = client.request("GET", f"/uploads/{client.room['room_id']}", headers=actor)
        assert rid not in {item["upload_id"] for item in listed.json()}
    for action in ("RESTORE", "ACTIVATE"):
        response = mutate(client, current, "lifecycle-transitions", {"action": action, "reason": "Recover"})
        assert response.status_code == 404, response.text
    with client.engine.connect() as db:
        assert db.scalar(text("SELECT lifecycle_state FROM records WHERE id=:r"), {"r": rid}) == lifecycle


def test_quarantine_cannot_be_changed_through_metadata_api(media_client):
    client = media_client
    row = create(client).json()
    response = client.request("PATCH", f"/records/{row['record_id']}/metadata",
        headers={**client.auth, "If-Match": '"1"'},
        json={"reason": "Operator policy", "changes": {"quarantined_record_ids": []}})
    assert response.status_code == 422


@pytest.mark.parametrize("visibility", ["FAMILY", "CUSTOM"])
def test_explicit_visibility_is_preserved(media_client, visibility):
    result = create(media_client, visibility=visibility)
    assert result.status_code == 200, result.text
    assert result.json()["visibility"] == visibility


def family_actor(client, name, role="member"):
    session, _, _ = client.signup(name, name + "@example.com")
    headers = {"Authorization": "Bearer " + session["access_token"]}
    joined = client.request("POST", "/families/join", headers=headers, json={
        "invite_code": client.room["invite_code"], "related_to_user_id": client.session["user"]["user_id"],
        "relation_to_related_user": "child"})
    assert joined.status_code == 200, joined.text
    with client.engine.begin() as db:
        db.execute(text("UPDATE family_members SET role=:role WHERE user_id=:u"), {"role": role, "u": session["user"]["user_id"]})
        member = db.scalar(text("SELECT id FROM family_members WHERE user_id=:u"), {"u": session["user"]["user_id"]})
    return headers, member


def test_admin_cannot_read_private_or_change_commons_title(media_client):
    client = media_client
    admin, _ = family_actor(client, "admin", "admin")
    private = create(client).json()
    denied = client.request("GET", f"/records/{private['record_id']}", headers=admin)
    missing = client.request("GET", f"/records/{uuid4()}", headers=admin)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    family = create(client, visibility="FAMILY").json()
    result = client.request("PATCH", f"/records/{family['record_id']}/metadata",
        headers={**admin, "If-Match": '"1"'}, json={"reason": "correct title", "changes": {"title": "changed"}})
    assert result.status_code == 403, result.text
    allowed = client.request("PATCH", f"/records/{family['record_id']}/metadata",
        headers={**admin, "If-Match": '"1"'}, json={"reason": "correct date", "changes": {"occurred_at": "2025-02-03"}})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["metadata"]["occurred_at"] == "2025-02-03"


def test_subject_restriction_survives_owner_publication_and_subject_removal(media_client):
    client = media_client
    daughter, member = family_actor(client, "daughter")
    sibling, _ = family_actor(client, "sibling")
    row = create(client, visibility="FAMILY", subject_member_ids=[member]).json()
    pending = mutate(client, row, "visibility-reductions", {"reason": "reduce", "allowed_member_ids": []}, **daughter)
    assert pending.status_code == 403
    reviewed = mutate(client, row, "reviews", {"reason": "verified subject"})
    assert reviewed.status_code == 200, reviewed.text
    restricted = mutate(client, reviewed.json(), "visibility-reductions", {"reason": "reduce", "allowed_member_ids": []}, **daughter)
    assert restricted.status_code == 200, restricted.text
    assert restricted.json()["lifecycle_state"] == "RESTRICTED"
    expanded = mutate(client, restricted.json(), "visibility-changes", {"reason": "share", "visibility": "FAMILY"})
    assert expanded.status_code == 200, expanded.text
    removed = client.request("PATCH", f"/records/{row['record_id']}/metadata", headers={**client.auth,
        "If-Match": f'"{expanded.json()["record_version"]}"'}, json={"reason": "correct person", "changes": {"subject_member_ids": []}})
    assert removed.status_code == 200, removed.text
    denied = client.request("GET", f"/records/{row['record_id']}", headers=sibling)
    assert denied.status_code == 404
    with client.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM record_restrictions WHERE record_id=:r AND active=true"), {"r": row["record_id"]}) == 1


def test_subject_deletion_request_is_not_purge_and_decline_preserves_restriction(media_client):
    client = media_client
    daughter, member = family_actor(client, "requester")
    row = create(client, visibility="FAMILY", subject_member_ids=[member]).json()
    row = mutate(client, row, "reviews", {"reason": "verify"}).json()
    row = mutate(client, row, "visibility-reductions", {"reason": "reduce", "allowed_member_ids": []}, **daughter).json()
    requested = mutate(client, row, "deletion-requests", {"reason": "please delete"}, **daughter)
    assert requested.status_code == 200, requested.text
    request_row = requested.json()
    decision = client.request("POST", f"/deletion-requests/{request_row['requests'][0]['id']}/decisions",
        headers={**client.auth, "If-Match": f'"{request_row["record_version"]}"', "Idempotency-Key": str(uuid4())},
        json={"reason": "retain original", "decision": "DECLINE"})
    assert decision.status_code == 200, decision.text
    assert decision.json()["lifecycle_state"] == "RESTRICTED"
    assert decision.json()["purge_status"] == "NOT_REQUESTED"


def test_legacy_metadata_roundtrip_and_restore(catalogue, monkeypatch):
    from alembic import command
    from app.api.routes.records import router
    from app.core import database
    from app.core.security import create_access_token
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from scripts.adopt_alembic import _build_alembic_config
    from sqlalchemy.orm import sessionmaker

    engine, url, _ = catalogue
    command.upgrade(_build_alembic_config(url), "head")
    uid, room, member = (str(uuid4()) for _ in range(3))
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id) VALUES (:u)"), {"u": uid})
        db.execute(text("INSERT INTO auth_identities(id,user_id,provider,provider_subject,payload_state) VALUES (:id,:u,'google',:sub,'LEGACY')"), {"id": str(uuid4()), "u": uid, "sub": uid})
        db.execute(text("INSERT INTO family_rooms(id,owner_user_id) VALUES (:r,:u)"), {"r": room, "u": uid})
        db.execute(text("INSERT INTO family_members(id,room_id,user_id,role) VALUES (:m,:r,:u,'owner')"), {"m": member, "r": room, "u": uid})
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer " + create_access_token(uid)}
        created = client.post("/api/v1/records", headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"room_id": room, "type": "text", "title": "legacy"})
        assert created.status_code == 200, created.text
        rid = created.json()["record_id"]
        edited = client.patch(f"/api/v1/records/{rid}/metadata", headers={**auth, "If-Match": '"1"'},
            json={"reason": "correct category", "changes": {"category": "TRAVEL"}})
        assert edited.status_code == 200, edited.text
        assert edited.json()["metadata"]["category"] == "TRAVEL"
        read = client.get(f"/api/v1/records/{rid}", headers=auth)
        assert read.json()["metadata"]["category"] == "TRAVEL"


def test_restore_does_not_accept_corrupted_ciphertext(media_client):
    client = media_client
    row = create(client).json()
    deleted = mutate(client, row, "lifecycle-transitions", {"action": "SOFT_DELETE", "reason": "delete"}).json()
    with client.engine.connect() as db:
        object_id = db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='record' AND owner_id=:r"), {"r": row["record_id"]})
    receipt, blob = client.objects[object_id]
    client.objects[object_id] = receipt, blob[:-1] + bytes([blob[-1] ^ 1])
    restored = mutate(client, deleted, "lifecycle-transitions", {"action": "RESTORE", "reason": "restore"})
    assert restored.status_code != 200
    with client.engine.connect() as db:
        assert db.scalar(text("SELECT lifecycle_state FROM records WHERE id=:r"), {"r": row["record_id"]}) == "SOFT_DELETED"


def test_semantic_correction_can_be_reprocessed_reviewed_and_reindexed(indexed_native):
    from test_secure_retrieval import index_secure, search_secure

    client, upload, job, _ = indexed_native
    rid, jid = upload["upload_id"], job["job_id"]
    before = client.request("GET", f"/records/{rid}", headers=client.auth)
    corrected = client.request("PATCH", f"/records/{rid}/metadata", headers={**client.auth, "If-Match": before.headers["etag"]},
        json={"reason": "correct date", "changes": {"occurred_at": "2025-04-03"}})
    assert corrected.status_code == 200, corrected.text
    assert search_secure(client).json()["hits"] == []
    approved = mutate(client, corrected.json(), "reviews", {"reason": "verify date"})
    assert approved.status_code == 200, approved.text
    retried = client.request("POST", f"/processing-jobs/{jid}/retry", headers=client.auth)
    assert retried.status_code == 200, retried.text
    ran = client.request("POST", f"/processing-jobs/{jid}/run", headers=client.auth)
    assert ran.status_code == 200 and ran.json()["status"] == "SUCCEEDED", ran.text
    reviewed = client.request("POST", f"/processing-jobs/{jid}/review", headers=client.auth, json={"action": "approve"})
    assert reviewed.status_code == 200, reviewed.text
    indexed = index_secure(client, upload, ran.json())
    assert indexed.status_code == 200, indexed.text
    assert {item["record_id"] for item in search_secure(client).json()["hits"]} == {rid}
