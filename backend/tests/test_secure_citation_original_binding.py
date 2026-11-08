"""SECURE citations resolve only their authenticated immutable original."""
import hashlib
from urllib.parse import urlsplit, parse_qs, urlencode
from uuid import UUID, uuid4
import pytest
from sqlalchemy import text
from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import media_client, upload, PNG
from test_secure_retrieval import processed, index_secure, search_secure, ask_secure
from app.retrieval.evaluation import SyntheticTokenHashProvider


def api_path(url):
    parts = urlsplit(url)
    return parts.path.removeprefix("/api/v1") + ("?" + parts.query if parts.query else "")


def test_old_citation_never_substitutes_replacement(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    answered = ask_secure(c)
    assert answered.status_code == 200 and answered.json()["gate_route"] == "ANSWER"
    citation = answered.json()["citations"][0]
    original_url = citation["original_url"]
    assert citation["source_object_id"] and citation["source_object_version"] == 1
    assert len(citation["source_ciphertext_sha256"]) == 64
    assert "source_sha256" not in citation
    before = c.request("GET", api_path(original_url), headers=c.auth)
    assert before.status_code == 200 and before.content == PNG
    replacement = upload(c, row, PNG + b"replacement-B")
    assert replacement["file_id"] == row["file_id"]
    assert search_secure(c).json()["hits"] == []
    stale = ask_secure(c)
    assert stale.status_code == 200 and stale.json()["gate_route"] == "NO_RECORD"
    response = c.request("GET", api_path(original_url), headers=c.auth)
    assert response.status_code == 409 and response.content != PNG + b"replacement-B", (
        "OLD CITATION SUBSTITUTED B", response.status_code, response.content)
    assert hashlib.sha256(response.content).digest() != hashlib.sha256(PNG + b"replacement-B").digest()
    print("A_SHA256=" + hashlib.sha256(PNG).hexdigest())
    print("B_SHA256=" + hashlib.sha256(PNG + b"replacement-B").hexdigest())
    path = "/processing-jobs/" + done["job_id"]
    assert c.request("POST", path + "/retry", headers=c.auth).status_code == 200
    completed = c.request("POST", path + "/run", headers=c.auth)
    assert completed.status_code == 200 and completed.json()["status"] == "SUCCEEDED"
    assert index_secure(c, replacement, completed.json()).status_code in (409, 503)
    assert c.request("POST", path + "/review", headers=c.auth, json={"action": "approve"}).status_code == 200
    assert index_secure(c, replacement, completed.json()).status_code == 200
    current = ask_secure(c).json()["citations"][0]
    assert current["source_object_id"] != citation["source_object_id"]
    assert current["source_ciphertext_sha256"] != citation["source_ciphertext_sha256"]
    opened = c.request("GET", api_path(current["original_url"]), headers=c.auth)
    assert opened.status_code == 200 and opened.content == PNG + b"replacement-B"
    assert c.request("GET", api_path(original_url), headers=c.auth).status_code == 409


@pytest.mark.parametrize("damage", ["missing", "partial", "file", "object", "version", "hash", "duplicate"])
def test_original_identity_rejected_before_original_io(media_client, monkeypatch, damage):
    c = media_client
    row = upload(c)
    url = urlsplit(row["file_url"])
    params = {key: value[0] for key, value in parse_qs(url.query).items()}
    oid = params["source_object_id"]
    if damage == "missing":
        params = {}
    elif damage == "partial":
        del params["source_object_id"]
    elif damage in ("file", "object"):
        params["file_id" if damage == "file" else "source_object_id"] = str(uuid4())
    elif damage == "version":
        params["source_object_version"] = "2"
    elif damage == "hash":
        params["expected_ciphertext_sha256"] = "0" * 64
    query = urlencode(params)
    if damage == "duplicate":
        query += "&file_id=" + params["file_id"]
    store = c.app.state.secure_runtime.store
    read = store.read
    def guarded(receipt):
        assert receipt.object_name != oid, "mismatched source read original"
        return read(receipt)
    monkeypatch.setattr(store, "read", guarded)
    response = c.request("GET", api_path(url.path + "?" + query), headers=c.auth)
    assert response.status_code == 409 and response.json()["detail"] == "STALE_SOURCE"
    assert PNG not in response.content


def test_citation_authentication_and_deleted_source(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    citation = ask_secure(c).json()["citations"][0]
    path = api_path(citation["original_url"])
    assert c.request("GET", path).status_code == 401
    other, _, _ = c.signup("foreign-b3", "foreign-b3@example.com")
    denied = c.request("GET", path, headers={"Authorization": "Bearer " + other["access_token"]})
    assert denied.status_code in (404, 503) and PNG not in denied.content
    assert c.request("DELETE", "/uploads/" + row["upload_id"], headers=c.auth).status_code == 200
    denied = c.request("GET", path, headers=c.auth)
    assert denied.status_code in (404, 409, 503) and PNG not in denied.content
    assert search_secure(c).json()["hits"] == []
    assert ask_secure(c).json()["gate_route"] == "NO_RECORD"


@pytest.mark.parametrize("damage", ["file_id", "object_id", "object_version"])
def test_authenticated_metadata_must_bind_original(media_client, damage):
    from app.api.routes.secure_media import record_source, record_payload, stage
    from app.api.routes.secure_operations import raw_json
    from app.services import secure_catalogue as cat
    c = media_client
    row = upload(c)
    runtime, uid = c.app.state.secure_runtime, c.session["user"]["user_id"]
    with c.engine.begin() as db:
        source = record_source(db, uid, row["upload_id"])
    metadata = record_payload(runtime, source)
    metadata["file"][damage] = 2 if damage == "object_version" else str(uuid4())
    staged = stage(runtime, uid, ("record", UUID(row["upload_id"])), raw_json(metadata))
    with c.engine.begin() as db:
        cat.publish_many(db, (staged,))
    response = c.request("GET", api_path(row["file_url"]), headers=c.auth)
    assert response.status_code in (409, 503) and PNG not in response.content


@pytest.mark.parametrize("race", ["file", "record"])
def test_original_read_revalidates_after_io(media_client, monkeypatch, race):
    c = media_client
    row = upload(c)
    params = parse_qs(urlsplit(row["file_url"]).query)
    oid = params["source_object_id"][0]
    store = c.app.state.secure_runtime.store
    read = store.read
    mutated = []
    def changed(receipt):
        blob = read(receipt)
        if receipt.object_name == oid:
            with c.engine.begin() as db:
                db.execute(text("UPDATE protected_pointers SET revision=revision+1,source_revision=source_revision+1 WHERE owner_type=:kind"), {"kind": race})
            mutated.append(True)
        return blob
    monkeypatch.setattr(store, "read", changed)
    response = c.request("GET", api_path(row["file_url"]), headers=c.auth)
    assert mutated and response.status_code in (409, 503) and PNG not in response.content
