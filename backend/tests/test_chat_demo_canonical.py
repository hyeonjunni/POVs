"""The live demo UI must use the canonical completeness and citation pipeline."""
from datetime import datetime
from app.ai import demo_service
from app.core import database
from app.models.upload import Upload
from test_grounded_answering_api import (
    rig, add_safety_sources, index, set_result, QUOTE, ROOM_ID, RECORD_ID,
    OTHER_RECORD_ID,
)


def chat(client, **extra):
    return client.post("/api/v1/ai/chat-demo", json={
        "room_id": ROOM_ID, "model_id": "exaone-45-33b-dgx",
        "persona_id": "father-calm", "query": "아버지 검진 몇 시?", **extra,
    })


def test_demo_51_record_attack_uses_canonical_completeness(rig, monkeypatch):
    tracker, embedding, provider, client = rig
    set_result(tracker, "아버지 검진 오전 9시")
    index(tracker, embedding)
    add_safety_sources(tracker, embedding, [
        *((f"job-demo-{n}", "아버지 검진 오전 9시") for n in range(49)),
        ("job-demo-conflict", "아버지의 검진은 오후 2시"),
    ])
    with tracker.session_maker() as db:
        for upload in db.query(Upload).filter(Upload.room_id == ROOM_ID):
            upload.created_at = datetime(2020 if upload.id == "record-job-demo-conflict" else 2021, 1, 1)
            upload.title = "아버지 검진"
            upload.description = ("아버지의 검진은 오후 2시"
                if upload.id == "record-job-demo-conflict" else "아버지 검진 오전 9시")
        db.commit()
    monkeypatch.setitem(client.app.dependency_overrides, database.get_db, tracker.get_db)
    monkeypatch.setattr(demo_service, "get_provider_for_model", lambda _: provider)
    for selected in (None, RECORD_ID):
        response = chat(client, selected_record_id=selected)
        assert response.status_code == 200, response.text
        assert response.json()["gate_route"] == "CLARIFY"
        assert response.json()["provider_mode"] == "not_called"
    assert provider.calls == 0
    assert provider.factory_calls == []


def test_demo_preserves_exact_citations_and_closed_provider_session(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    response = chat(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gate_route"] == "ANSWER"
    assert QUOTE in body["answer"]
    assert body["citations"][0]["record_id"] == RECORD_ID
    assert body["citations"][0]["original_url"]
    assert provider.open_count_at_generate == [0]


def test_demo_rejects_foreign_selected_record_without_inference(rig):
    tracker, embedding, provider, client = rig
    for selected in (OTHER_RECORD_ID, "record-outside-room"):
        response = chat(client, selected_record_id=selected)
        assert response.status_code == 404
    assert provider.calls == 0


def test_demo_revalidates_source_after_inference(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    provider.mutation = lambda _: set_result(tracker, "changed")
    assert chat(client).status_code == 409


def test_demo_rejects_missing_identity_before_inference(rig):
    from app.models.auth_identity import AuthIdentity
    from test_grounded_answering_api import USER_ID
    tracker, embedding, provider, client = rig
    with tracker.session_maker() as db:
        db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
        db.commit()
    for selected in (None, RECORD_ID):
        assert chat(client, selected_record_id=selected).status_code == 401
    assert provider.calls == 0


def test_demo_rejects_blank_query(rig):
    _tracker, _embedding, provider, client = rig
    assert chat(client, query="   ").status_code == 422
    assert provider.calls == 0
