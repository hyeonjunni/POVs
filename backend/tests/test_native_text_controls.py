import pytest
from test_native_text_pipeline import native_case as _native_case
from test_secure_catalogue import catalogue as _catalogue
from test_secure_identity import identity_client as _identity_client
from test_secure_media import media_client as _media_client
from test_secure_retrieval import index_secure, search_secure

catalogue = _catalogue
identity_client = _identity_client
media_client = _media_client
native_case = _native_case


@pytest.mark.parametrize("body", [b"", b" \n\t", b"\xff\xfe", b"native\x00text"])
def test_invalid_text_bytes_are_rejected_before_storage(media_client, body):
    client = media_client
    created = client.request("POST", "/uploads", headers=client.auth, json={
        "room_id": client.room["room_id"], "type": "text", "title": "Synthetic"})
    rid = created.json()["upload_id"]
    before = len(client.objects)
    response = client.request("POST", f"/uploads/{rid}/binary", headers=client.auth,
        files={"file": ("source.txt", body, "text/plain")})
    assert response.status_code == 400, response.text
    assert len(client.objects) == before


def test_text_limit_is_measured_and_configurable(media_client, monkeypatch):
    from app.core.config import settings
    client = media_client
    monkeypatch.setattr(settings, "text_max_input_bytes", 4)
    created = client.request("POST", "/uploads", headers=client.auth, json={
        "room_id": client.room["room_id"], "type": "text", "title": "Synthetic"})
    response = client.request("POST", f"/uploads/{created.json()['upload_id']}/binary",
        headers=client.auth, files={"file": ("source.txt", b"12345", "text/plain")})
    assert response.status_code == 413, response.text


def test_rejected_native_result_stays_out_of_search(native_case):
    client, row, done = native_case
    jid = done["job_id"]
    response = client.request("POST", f"/processing-jobs/{jid}/review", headers=client.auth,
        json={"action": "reject"})
    assert response.status_code == 200, response.text
    assert index_secure(client, row, done).status_code in (409, 503)
    assert search_secure(client).json()["hits"] == []


def test_other_member_cannot_publish_or_approve_owner_text(native_case):
    client, row, done = native_case
    sibling, _, _ = client.signup("sibling-text", "sibling-text@example.com")
    headers = {"Authorization": "Bearer " + sibling["access_token"]}
    joined = client.request("POST", "/families/join", headers=headers, json={
        "invite_code": client.room["invite_code"],
        "related_to_user_id": client.session["user"]["user_id"],
        "relation_to_related_user": "child"})
    assert joined.status_code == 200, joined.text
    published = client.request("POST", f"/uploads/{row['upload_id']}/publish-text", headers=headers)
    assert published.status_code in (403, 404), published.text
    approved = client.request("POST", f"/processing-jobs/{done['job_id']}/review",
        headers=headers, json={"action": "approve"})
    assert approved.status_code == 404, approved.text
