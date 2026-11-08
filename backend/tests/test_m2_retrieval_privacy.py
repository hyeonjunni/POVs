from uuid import uuid4

import pytest
from sqlalchemy import text
from test_native_text_pipeline import indexed_native as _indexed_native
from test_native_text_pipeline import native_case as _native_case
from test_secure_catalogue import catalogue as _catalogue
from test_secure_identity import identity_client as _identity_client
from test_secure_media import media_client as _media_client
from test_secure_retrieval import ask_secure, search_secure

indexed_native = _indexed_native
native_case = _native_case
catalogue = _catalogue
identity_client = _identity_client
media_client = _media_client


@pytest.fixture
def privacy_case(indexed_native):
    client, row, _, _ = indexed_native
    session, _, _ = client.signup("privacy-viewer", "privacy-viewer@example.com")
    viewer_auth = {"Authorization": "Bearer " + session["access_token"]}
    joined = client.request("POST", "/families/join", headers=viewer_auth, json={
        "invite_code": client.room["invite_code"],
        "related_to_user_id": client.session["user"]["user_id"],
        "relation_to_related_user": "child"})
    assert joined.status_code == 200, joined.text
    with client.engine.connect() as db:
        member = db.scalar(text("SELECT id FROM family_members WHERE user_id=:u AND room_id=:r"),
            {"u": session["user"]["user_id"], "r": client.room["room_id"]})
    return client, row["upload_id"], viewer_auth, member


def mutate(client, record_id, operation, **payload):
    current = client.request("GET", f"/records/{record_id}/management", headers=client.auth)
    assert current.status_code == 200, current.text
    response = client.request("POST", f"/records/{record_id}/{operation}",
        headers=client.auth | {"If-Match": current.headers["etag"], "Idempotency-Key": str(uuid4())},
        json={"reason": "Synthetic privacy verification", **payload})
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("arm", ["substring", "bm25", "vector", "hybrid_rrf"])
def test_private_is_filtered_before_decryption_and_candidates(privacy_case, monkeypatch, arm):
    client, rid, viewer, _ = privacy_case
    mutate(client, rid, "visibility-changes", visibility="PRIVATE")
    assert search_secure(client, arm=arm).json()["hits"]
    client.auth = viewer
    from app.services import secure_retrieval
    monkeypatch.setattr(secure_retrieval, "record_source",
        lambda *a, **k: pytest.fail("Unauthorized record reached source loading"))
    monkeypatch.setattr(secure_retrieval, "open_bytes",
        lambda *a, **k: pytest.fail("Unauthorized index reached decryption"))

    response = search_secure(client, arm=arm)

    assert response.status_code == 200, response.text
    assert response.json()["hits"] == []
    assert rid not in response.text


@pytest.mark.parametrize("admin", [False, True])
def test_private_and_absent_have_identical_no_record_payload(privacy_case, monkeypatch, admin):
    client, rid, viewer, member = privacy_case
    mutate(client, rid, "visibility-changes", visibility="PRIVATE")
    if admin:
        with client.engine.begin() as db:
            db.execute(text("UPDATE family_members SET role='admin' WHERE id=:m"), {"m": member})
    client.auth = viewer
    from app.ai.grounded_answering import SyntheticPlanProvider
    monkeypatch.setattr(SyntheticPlanProvider, "generate",
        lambda *a, **k: pytest.fail("PRIVATE-only evidence invoked LLM"))
    hidden = client.request("GET", f"/records/{rid}", headers=viewer)
    absent = client.request("GET", f"/records/{uuid4()}", headers=viewer)
    existing_answer = ask_secure(client)
    existing_search = search_secure(client)
    with client.engine.begin() as db:
        db.execute(text("DELETE FROM records WHERE id=:r"), {"r": rid})

    absent_answer = ask_secure(client)
    absent_search = search_secure(client)

    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()
    assert existing_answer.status_code == absent_answer.status_code == 200
    assert existing_answer.json() == absent_answer.json()
    assert existing_search.json() == absent_search.json()
    assert existing_answer.json()["gate_route"] == "NO_RECORD"
    assert existing_answer.json()["provider_mode"] == "not_called"
    assert existing_answer.json()["citations"] == []


def test_custom_grant_revocation_removes_retrieval_and_original(privacy_case):
    client, rid, viewer, member = privacy_case
    owner = client.auth
    mutate(client, rid, "visibility-changes", visibility="CUSTOM", custom_grants=[member])
    client.auth = viewer
    answer = ask_secure(client)
    assert answer.status_code == 200, answer.text
    assert answer.json()["gate_route"] == "ANSWER"
    original = answer.json()["citations"][0]["original_url"].removeprefix("/api/v1")
    assert client.request("GET", original, headers=viewer).status_code == 200
    client.auth = owner

    mutate(client, rid, "visibility-changes", visibility="CUSTOM", custom_grants=[])

    client.auth = viewer
    assert search_secure(client).json()["hits"] == []
    assert ask_secure(client).json()["gate_route"] == "NO_RECORD"
    assert client.request("GET", original, headers=viewer).status_code == 404


def test_operator_quarantine_removes_owner_retrieval_and_answers(privacy_case, monkeypatch):
    from uuid import UUID
    from app.core.config import settings

    client, rid, _, _ = privacy_case
    assert search_secure(client).json()["hits"]
    monkeypatch.setattr(settings, "quarantined_record_ids", [UUID(rid)])

    assert search_secure(client).json()["hits"] == []
    assert ask_secure(client).json()["gate_route"] == "NO_RECORD"


@pytest.mark.parametrize("action", ["HIDE", "ARCHIVE", "SOFT_DELETE"])
def test_inactive_record_excluded_and_restore_requires_review(privacy_case, action):
    client, rid, viewer, _ = privacy_case
    original = ask_secure(client).json()["citations"][0]["original_url"].removeprefix("/api/v1")
    mutate(client, rid, "lifecycle-transitions", action=action)
    assert search_secure(client).json()["hits"] == []
    assert ask_secure(client).json()["gate_route"] == "NO_RECORD"
    assert client.request("GET", original, headers=viewer).status_code == 404

    restored = mutate(client, rid, "lifecycle-transitions", action="RESTORE")

    assert restored.json()["lifecycle_state"] == "ARCHIVED"
    assert restored.json()["review_status"] == "PENDING_REVIEW"
    assert search_secure(client).json()["hits"] == []
    assert ask_secure(client).json()["gate_route"] == "NO_RECORD"
    assert client.request("GET", original, headers=client.auth).status_code != 200


def test_subject_reduction_removes_browse_and_all_rag(privacy_case):
    client, rid, viewer, member = privacy_case
    current = client.request("GET", f"/records/{rid}", headers=client.auth)
    patched = client.request("PATCH", f"/records/{rid}/metadata",
        headers=client.auth | {"If-Match": current.headers["etag"]},
        json={"changes": {"subject_member_ids": [member]}, "reason": "Verified subject"})
    assert patched.status_code == 200, patched.text
    mutate(client, rid, "reviews")
    owner = client.auth
    client.auth = viewer

    reduced = mutate(client, rid, "visibility-reductions", allowed_member_ids=[member])

    assert reduced.json()["lifecycle_state"] == "RESTRICTED"
    for auth in (viewer, owner):
        client.auth = auth
        assert client.request("GET", "/uploads/" + client.room["room_id"], headers=auth).json() == []
        assert search_secure(client).json()["hits"] == []
        assert ask_secure(client).json()["gate_route"] == "NO_RECORD"


def test_unauthorized_records_do_not_consume_corpus_limit(privacy_case, monkeypatch):
    client, rid, viewer, _ = privacy_case
    mutate(client, rid, "visibility-changes", visibility="PRIVATE")
    with client.engine.begin() as db:
        for _ in range(1001):
            clone = str(uuid4())
            db.execute(text("INSERT INTO uploads(id,room_id,uploader_user_id,type,status) SELECT :id,room_id,uploader_user_id,type,status FROM uploads WHERE id=:source"), {"id": clone, "source": rid})
            db.execute(text("INSERT INTO records(id,room_id,owner_member_id,uploader_member_id,visibility,review_status) SELECT :id,room_id,owner_member_id,uploader_member_id,'PRIVATE','APPROVED' FROM records WHERE id=:source"), {"id": clone, "source": rid})
            db.execute(text("INSERT INTO protected_pointers(owner_type,owner_id,domain,scope_id,object_id,object_version,state,source_revision,deletion_epoch,revision,authorization_epoch) SELECT owner_type,:id,domain,scope_id,object_id,object_version,state,source_revision,deletion_epoch,revision,authorization_epoch FROM protected_pointers WHERE owner_type='record' AND owner_id=:source"), {"id": clone, "source": rid})
    client.auth = viewer
    from app.services import secure_retrieval
    monkeypatch.setattr(secure_retrieval, "record_source",
        lambda *a, **k: pytest.fail("Hidden record consumed corpus capacity"))

    response = ask_secure(client)

    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "NO_RECORD"
    assert response.json()["provider_mode"] == "not_called"


def test_archive_during_generation_blocks_answer_publication(privacy_case, monkeypatch):
    client, rid, _, _ = privacy_case
    from app.ai.grounded_answering import SyntheticPlanProvider
    original_generate = SyntheticPlanProvider.generate

    def archive_during_generation(self, *args, **kwargs):
        mutate(client, rid, "lifecycle-transitions", action="ARCHIVE")
        return original_generate(self, *args, **kwargs)

    monkeypatch.setattr(SyntheticPlanProvider, "generate", archive_during_generation)

    response = ask_secure(client)

    assert response.status_code in (404, 409, 503), response.text
    assert rid not in response.text
    assert "아버지 검진은 오후 2시" not in response.text


def test_title_correction_preserves_search_and_existing_citation(indexed_native):
    client, row, done, _ = indexed_native
    rid = row["upload_id"]
    answer = ask_secure(client)
    assert answer.status_code == 200, answer.text
    original = answer.json()["citations"][0]["original_url"].removeprefix("/api/v1")
    before = client.request("GET", f"/records/{rid}", headers=client.auth)

    edited = client.request("PATCH", f"/records/{rid}/metadata",
        headers=client.auth | {"If-Match": before.headers["etag"]},
        json={"changes": {"title": "Corrected display title"}, "reason": "Display correction"})

    assert edited.status_code == 200, edited.text
    assert edited.json()["source_revision"] == before.json()["source_revision"]
    assert edited.json()["authorization_epoch"] == before.json()["authorization_epoch"]
    search = search_secure(client)
    assert search.status_code == 200, search.text
    assert {hit["record_id"] for hit in search.json()["hits"]} == {rid}
    assert ask_secure(client).json()["gate_route"] == "ANSWER"
    opened = client.request("GET", original, headers=client.auth)
    assert opened.status_code == 200, opened.text
    assert opened.content.decode() == "아버지 검진은 오후 2시였다."
    job = client.request("GET", f"/processing-jobs/{done['job_id']}", headers=client.auth)
    assert job.status_code == 200, job.text
    assert job.json()["status"] == "SUCCEEDED"


def test_original_replacement_advances_canonical_versions(indexed_native):
    client, row, _, _ = indexed_native
    rid = row["upload_id"]
    before = client.request("GET", f"/records/{rid}", headers=client.auth).json()

    replaced = client.request("POST", f"/uploads/{rid}/binary", headers=client.auth,
        files={"file": ("changed.txt", "아버지 검진은 오후 4시였다.".encode(), "text/plain")})

    assert replaced.status_code == 200, replaced.text
    after = client.request("GET", f"/records/{rid}", headers=client.auth).json()
    assert after["source_revision"] == before["source_revision"] + 1
    assert after["record_version"] == before["record_version"] + 1
    assert after["authorization_epoch"] > before["authorization_epoch"]


def test_private_access_denial_is_audited_without_plaintext(privacy_case):
    client, rid, viewer, _ = privacy_case
    mutate(client, rid, "visibility-changes", visibility="PRIVATE")

    denied = client.request("GET", f"/records/{rid}", headers=viewer)

    assert denied.status_code == 404, denied.text
    with client.engine.connect() as db:
        rows = db.execute(text("SELECT * FROM record_audits WHERE action='ACCESS_DENIED' AND record_id=:r"), {"r": rid}).mappings().all()
    assert rows
    stored = str(rows)
    assert "아버지 검진은 오후 2시" not in stored
    assert "Synthetic appointment" not in stored
    assert viewer["Authorization"] not in stored


@pytest.mark.parametrize("change", [
    {"active": False}, {"superseded_by": "claim-next"}, {"claim_version": 999},
])
def test_inactive_superseded_or_stale_claim_cannot_render(change):
    from dataclasses import replace

    from app.ai.grounded_answering import (
        GroundedEvidenceChanged,
        build_answer_plan,
        build_fact_sheet,
        render_plan,
    )
    from test_grounded_answering import chunk

    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    claim = plan.claims[0]
    assert claim.claim_version == plan.facts[0].source.source_attempt + 1
    assert render_plan(plan, [claim.claim_id])[0]
    stale = replace(plan, claims=(replace(claim, **change),))

    with pytest.raises(GroundedEvidenceChanged, match="STALE_SOURCE"):
        render_plan(stale, [claim.claim_id])
