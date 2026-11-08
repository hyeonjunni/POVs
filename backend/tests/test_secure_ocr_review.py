"""Actual SECURE uploader authority and source-bound OCR review."""
import pytest
from sqlalchemy import text
from test_secure_catalogue import catalogue
from test_secure_identity import identity_client, create_room
from test_secure_media import media_client, upload, content_path
from test_secure_retrieval import processed, index_secure, search_secure, ask_secure, QUOTE, ARMS
from app.retrieval.evaluation import SyntheticTokenHashProvider

def review(c, done, action=None, headers=None):
    path = "/processing-jobs/" + done["job_id"] + "/review"
    return c.request("GET" if action is None else "POST", path,
        headers=c.auth if headers is None else headers,
        **({} if action is None else {"json": {"action": action}}))

def historical_index(c, row, done):
    """Model an index persisted by the old absent-review admission bug."""
    assert review(c, done, "approve").status_code == 200
    assert index_secure(c, row, done).status_code == 200
    old = ask_secure(c).json()["citations"][0]["original_url"].removeprefix("/api/v1")
    with c.engine.begin() as db:
        db.execute(text("UPDATE processing_jobs SET review_json=NULL WHERE id=:j"), {"j": done["job_id"]})
    return old


def test_actual_binary_uploader_is_persisted(media_client):
    c = media_client
    row = upload(c)
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT file_uploader_user_id FROM upload_files WHERE id=:f"),
            {"f": row["file_id"]}) == c.session["user"]["user_id"]

def test_uploader_review_approve_and_reject_evidence(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, approved=False)
    assert review(c, done).status_code == 200
    assert review(c, done).json()["review_status"] == "unreviewed"
    old = historical_index(c, row, done)
    assert review(c, done, "reject").status_code == 200
    assert review(c, done, "reject").status_code == 200
    assert review(c, done, "approve").status_code == 409
    assert review(c, done).json()["review_status"] == "rejected"
    for arm in ARMS:
        response = search_secure(c, arm)
        assert response.status_code == 200 and response.json()["hits"] == []
    assert ask_secure(c).json()["citations"] == []
    assert c.request("GET", old, headers=c.auth).status_code == 409
    assert c.request("GET", content_path(row), headers=c.auth).status_code == 200
    assert index_secure(c, row, done).status_code in (409, 503)
    approved, done2 = processed(c, monkeypatch, approved=False)
    assert review(c, done2, "approve").json()["review_status"] == "confirmed"
    assert review(c, done2, "approve").status_code == 200
    assert review(c, done2, "reject").status_code == 409
    assert index_secure(c, approved, done2).status_code == 200
    assert search_secure(c).json()["hits"][0]["text"] == QUOTE
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT result_json FROM processing_jobs WHERE id=:j"), {"j": done["job_id"]}) is None

def test_review_actual_uploader_not_room_owner(media_client, monkeypatch):
    c = media_client
    owner_auth = c.auth
    member, _, _ = c.signup("member", "member@example.com")
    foreign, _, _ = c.signup("foreign", "foreign@example.com")
    c.auth = {"Authorization": "Bearer " + member["access_token"]}
    joined = c.request("POST", "/families/join", headers=c.auth, json={
        "invite_code": c.room["invite_code"], "related_to_user_id": c.session["user"]["user_id"],
        "relation_to_related_user": "child"})
    assert joined.status_code == 200
    row, done = processed(c, monkeypatch, approved=False)
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT file_uploader_user_id FROM upload_files WHERE id=:f"), {"f": row["file_id"]}) == member["user"]["user_id"]
    for headers, status in [(owner_auth, 404), ({"Authorization": "Bearer " + foreign["access_token"]}, 404), ({}, 401)]:
        for action in [None, "approve", "reject"]:
            result = review(c, done, action, headers)
            assert result.status_code == status, result.text
    denied = c.request("POST", "/records/" + row["upload_id"] + "/retrieval-index",
                       headers=owner_auth, json={"processing_job_id": done["job_id"]})
    assert denied.status_code in (409, 503)
    assert review(c, done, "approve").status_code == 200
    with c.engine.begin() as db:
        db.execute(text("UPDATE upload_files SET file_uploader_user_id=NULL WHERE id=:f"), {"f": row["file_id"]})
    for action in [None, "approve", "reject"]:
        assert review(c, done, action).status_code == 409

@pytest.mark.parametrize("actor,status", [("owner", 503), ("member", 403)])
def test_failed_or_forbidden_replacement_preserves_byte_uploader(media_client, monkeypatch, actor, status):
    c = media_client
    row = upload(c)
    owner_auth = c.auth
    owner = c.session["user"]["user_id"]
    member, _, _ = c.signup("replacement", "replacement@example.com")
    c.auth = {"Authorization": "Bearer " + member["access_token"]}
    assert c.request("POST", "/families/join", headers=c.auth, json={
        "invite_code": c.room["invite_code"], "related_to_user_id": owner,
        "relation_to_related_user": "child"}).status_code == 200
    store = c.app.state.secure_runtime.store
    calls = []
    def failed_create(*args):
        calls.append(args)
        raise ValueError("synthetic failure")
    monkeypatch.setattr(store, "create", failed_create)
    if actor == "owner":
        c.auth = owner_auth
    from test_secure_media import PNG
    response = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=c.auth,
        files={"file": ("synthetic.png", PNG, "image/png")})
    assert response.status_code == status
    assert bool(calls) is (actor == "owner")
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT file_uploader_user_id FROM upload_files WHERE id=:f"), {"f": row["file_id"]}) == owner

@pytest.mark.parametrize("identity_client", ["0005_secure_catalogue", "0009_ocr_review"], indirect=True)
def test_old_schema_binary_upload_fails_before_provenance_or_storage(identity_client):
    from uuid import uuid4
    from test_secure_media import PNG
    c = identity_client
    session, _, _ = c.signup()
    auth = {"Authorization": "Bearer " + session["access_token"]}
    from test_secure_catalogue import scope, key, envelope, put
    uid, record_id = session["user"]["user_id"], str(uuid4())
    with c.engine.begin() as db:
        room = scope(db)
        kid = key(db, room, state="ACTIVE")
        put(db, "family_rooms", id=room, owner_user_id=uid)
        put(db, "family_members", id=str(uuid4()), room_id=room, user_id=uid, role="owner")
        put(db, "uploads", id=record_id, room_id=room, uploader_user_id=uid, type="image", status="uploaded")
        oid = envelope(db, room, kid, record_id=record_id, artifact_kind="record_metadata", state="ACTIVE", exact_version=str(uuid4()))
        put(db, "protected_pointers", owner_type="record", owner_id=record_id, domain="room", scope_id=room,
            object_id=oid, object_version=1, state="ENCRYPTED_ACTIVE", source_revision=1,
            deletion_epoch=0, authorization_epoch=0, revision=1)
    c.refresh()
    objects = dict(c.objects)
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT state FROM protected_pointers WHERE owner_type='record' AND owner_id=:r"), {"r": record_id}) == "ENCRYPTED_ACTIVE"
    result = c.request("POST", "/uploads/" + record_id + "/binary", headers=auth,
        files={"file": ("synthetic.png", PNG, "image/png")})
    assert result.status_code == 409
    assert "schema" in result.json()["detail"]
    assert result.headers["cache-control"] == "no-store"
    assert c.objects == objects
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM upload_files")) == 0

@pytest.mark.parametrize("damage", ["attempt", "digest", "ciphertext", "failed", "pending"])
def test_review_rejects_stale_or_unavailable_result(media_client, monkeypatch, damage):
    c = media_client
    row, done = processed(c, monkeypatch, approved=False)
    if damage == "ciphertext":
        with c.engine.connect() as db:
            oid = db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='job' AND owner_id=:j"), {"j": done["job_id"]})
        receipt, blob = c.objects[oid]
        c.objects[oid] = receipt, blob[:-1] + bytes([blob[-1] ^ 1])
    else:
        with c.engine.begin() as db:
            if damage == "attempt":
                db.execute(text("UPDATE processing_jobs SET attempt=attempt+1 WHERE id=:j"), {"j": done["job_id"]})
            elif damage == "digest":
                import json
                db.execute(text("UPDATE processing_jobs SET review_json=:r WHERE id=:j"), {
                    "j": done["job_id"], "r": json.dumps({"status": "confirmed", "source_attempt": done["attempt"], "source_result_sha256": "0"*64})})
            else:
                db.execute(text("UPDATE processing_jobs SET status=:s WHERE id=:j"), {"j": done["job_id"], "s": damage.upper()})
    for action in [None, "approve", "reject"]:
        assert review(c, done, action).status_code in (409, 503)

@pytest.mark.parametrize("prior_review", [None, "reject"])
def test_owner_replacement_preserves_attribution_and_requires_new_review(media_client, monkeypatch, prior_review):
    c = media_client
    row, done = processed(c, monkeypatch, approved=False)
    if prior_review is not None:
        assert review(c, done, prior_review).status_code == 200
    owner_auth = c.auth
    owner = c.session["user"]["user_id"]
    member, _, _ = c.signup("replacer", "replacer@example.com")
    c.auth = {"Authorization": "Bearer " + member["access_token"]}
    assert c.request("POST", "/families/join", headers=c.auth, json={
        "invite_code": c.room["invite_code"], "related_to_user_id": owner,
        "relation_to_related_user": "child"}).status_code == 200
    member_auth = c.auth
    from test_secure_media import PNG
    denied = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=member_auth,
        files={"file": ("synthetic.png", PNG, "image/png")})
    assert denied.status_code == 403
    c.auth = owner_auth
    replacement = upload(c, row)
    assert replacement["file_id"] == row["file_id"]
    assert replacement["uploader_user_id"] == owner
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT file_uploader_user_id FROM upload_files WHERE id=:f"), {"f": row["file_id"]}) == owner
    path = "/processing-jobs/" + done["job_id"]
    assert c.request("POST", path + "/retry", headers=c.auth).status_code == 200
    current = c.request("POST", path + "/run", headers=c.auth)
    assert current.status_code == 200 and current.json()["status"] == "SUCCEEDED"
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    assert index_secure(c, replacement, current.json()).status_code in (409, 503)
    assert review(c, current.json(), "approve", member_auth).status_code == 404
    assert review(c, current.json(), "approve").status_code == 200

def test_review_invalidation_is_atomic(media_client, monkeypatch):
    from app.api.routes import secure_media
    from app.services import secure_catalogue as cat
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, approved=False)
    historical_index(c, row, done)
    monkeypatch.setattr(secure_media, "invalidate_pointers",
        lambda *a: (_ for _ in ()).throw(cat.SecureStateChanged()))
    assert review(c, done, "reject").status_code == 503
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT review_json FROM processing_jobs WHERE id=:j"), {"j": done["job_id"]}) is None
        assert db.scalar(text("SELECT state FROM protected_pointers WHERE owner_type='record_index' AND owner_id=:r"), {"r": row["upload_id"]}) == "ENCRYPTED_ACTIVE"
    assert search_secure(c).status_code in (409, 503)

@pytest.mark.parametrize("race", ["uploader", "attempt", "review"])
def test_review_revalidates_after_result_decryption(media_client, monkeypatch, race):
    from app.api.routes import secure_media
    c = media_client
    row, done = processed(c, monkeypatch, approved=False)
    original = secure_media.open_bytes
    def change(runtime, values):
        data = original(runtime, values)
        if values[0].artifact_kind == "processing_result":
            with c.engine.begin() as db:
                if race == "uploader":
                    db.execute(text("UPDATE upload_files SET file_uploader_user_id=NULL WHERE id=:f"), {"f": row["file_id"]})
                elif race == "attempt":
                    db.execute(text("UPDATE processing_jobs SET attempt=attempt+1 WHERE id=:j"), {"j": done["job_id"]})
                else:
                    db.execute(text("UPDATE processing_jobs SET review_json='{}' WHERE id=:j"), {"j": done["job_id"]})
        return data
    monkeypatch.setattr(secure_media, "open_bytes", change)
    assert review(c, done, "approve").status_code in (409, 503)
    with c.engine.connect() as db:
        assert "confirmed" not in (db.scalar(text("SELECT review_json FROM processing_jobs WHERE id=:j"), {"j": done["job_id"]}) or "")

@pytest.mark.parametrize("damage", ["missing", "duplicate", "attempt", "digest"])
def test_citation_review_binding_is_required_when_present(media_client, monkeypatch, damage):
    from urllib.parse import urlsplit, parse_qsl, urlencode
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, approved=False)
    assert review(c, done, "approve").status_code == 200
    assert index_secure(c, row, done).status_code == 200
    original = ask_secure(c).json()["citations"][0]["original_url"]
    url = urlsplit(original)
    params = parse_qsl(url.query)
    if damage == "missing":
        params = [(k, v) for k, v in params if k != "source_attempt"]
    elif damage == "duplicate":
        params += [("source_attempt", "0")]
    else:
        field, value = ("source_attempt", "99") if damage == "attempt" else ("source_result_sha256", "0"*64)
        params = [(k, value if k == field else v) for k, v in params]
    assert c.request("GET", url.path.removeprefix("/api/v1") + "?" + urlencode(params), headers=c.auth).status_code == 409


def test_rejected_before_first_index_never_becomes_evidence(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, approved=False)
    assert review(c, done, "reject").status_code == 200
    assert index_secure(c, row, done).status_code in (409, 503)
    for arm in ARMS:
        response = search_secure(c, arm)
        assert response.status_code == 200 and response.json()["hits"] == []
    assert ask_secure(c).json()["citations"] == []


def test_absent_approval_cannot_become_evidence(media_client, monkeypatch):
    """Regression for the physical mobile indexing bypass."""
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, approved=False)
    state = review(c, done).json()
    assert state["review_status"] == "unreviewed"
    assert state["user_confirmed"] is False
    assert index_secure(c, row, done).status_code in (409, 503)
    assert search_secure(c).json()["hits"] == []
    assert ask_secure(c).json()["citations"] == []


@pytest.mark.parametrize("state", ["unreviewed", "rejected", "attempt", "result", "both"])
def test_secure_index_requires_current_confirmed_review(media_client, monkeypatch, state):
    import json
    from ocr_approval_helpers import confirmed_review
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, approved=False)
    metadata = json.loads(confirmed_review(done["result"], done["attempt"]))
    if state in ("unreviewed", "rejected"):
        metadata["status"] = state
    if state in ("attempt", "both"):
        metadata["source_attempt"] += 1
    if state in ("result", "both"):
        metadata["source_result_sha256"] = "0" * 64
    with c.engine.begin() as db:
        db.execute(text("UPDATE processing_jobs SET review_json=:r WHERE id=:j"),
                   {"r": json.dumps(metadata), "j": done["job_id"]})
    assert index_secure(c, row, done).status_code in (409, 503)
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM protected_pointers WHERE owner_type='record_index' AND owner_id=:r"),
                         {"r": row["upload_id"]}) == 0
    assert search_secure(c).json()["hits"] == []
    assert ask_secure(c).json()["citations"] == []
