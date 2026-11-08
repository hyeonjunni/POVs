import pytest

from test_m2_retrieval_privacy import (
    privacy_case as _privacy_case, indexed_native as _indexed_native,
    native_case as _native_case, catalogue as _catalogue,
    identity_client as _identity_client, media_client as _media_client, mutate,
)
from test_secure_retrieval import (
    RECORD_ANSWER, ask_secure, record_answer_provider as _record_answer_provider,
)
from test_secure_identity import create_room
from test_native_text_pipeline import QUOTE

privacy_case = _privacy_case
indexed_native = _indexed_native
native_case = _native_case
catalogue = _catalogue
identity_client = _identity_client
media_client = _media_client
record_answer_provider = _record_answer_provider


def test_generic_puppet_topic_does_not_resolve_as_brother(media_client, monkeypatch, record_answer_provider):
    from test_secure_retrieval import processed, index_secure
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    client = media_client
    client.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(client, monkeypatch, quote="인형극 공연 기록이다.")
    assert index_secure(client, row, done).status_code == 200
    response = ask_secure(client, "인형극 기록 보여줘", arm="bm25")
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "ANSWER"
    assert {c["record_id"] for c in response.json()["citations"]} == {row["upload_id"]}
    assert response.json()["provider_mode"] == "local"
    assert response.json()["gate_action"] == "llm_generated"
    assert response.json()["answer"] == RECORD_ANSWER
    assert len(record_answer_provider) == 1
    request = record_answer_provider[0]
    assert request.user_query == "인형극 기록 보여줘"
    assert response.json()["query_context"]["address_bindings"] == []
    assert "인형극 공연 기록이다." in "\n".join(request.prompt_package["retrieved_evidence"])


def test_discovery_secure_original_and_family_isolation(privacy_case, record_answer_provider):
    client, record_id, viewer, _ = privacy_case
    response = ask_secure(client, "검진은 기록 보여줘", arm="bm25")
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["gate_route"] == "ANSWER" and answer["provider_mode"] == "local"
    assert answer["gate_action"] == "llm_generated"
    assert answer["answer"] == RECORD_ANSWER
    assert len(record_answer_provider) == 1
    assert {c["record_id"] for c in answer["citations"]} == {record_id}
    original = answer["citations"][0]["original_url"].removeprefix("/api/v1")
    opened = client.request("GET", original, headers=viewer)
    assert opened.status_code == 200
    assert opened.content.decode() == QUOTE
    assert opened.content.decode() in "\n".join(record_answer_provider[0].prompt_package["retrieved_evidence"])
    client.room, _ = create_room(client, client.session)
    response = ask_secure(client, "검진은 기록 보여줘", arm="bm25")
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "NO_RECORD"
    assert response.json()["citations"] == []
    assert response.json()["provider_mode"] == "not_called"
    assert len(record_answer_provider) == 1
    assert record_id not in response.text


@pytest.mark.parametrize("visibility", ["PRIVATE", "CUSTOM"])
def test_discovery_denied_sources_never_decrypted(privacy_case, monkeypatch, visibility):
    client, record_id, viewer, _ = privacy_case
    payload = {"custom_grants": []} if visibility == "CUSTOM" else {}
    mutate(client, record_id, "visibility-changes", visibility=visibility, **payload)
    client.auth = viewer
    from app.services import secure_retrieval
    monkeypatch.setattr(secure_retrieval, "record_source", lambda *a: pytest.fail("denied record loaded"))
    monkeypatch.setattr(secure_retrieval, "open_bytes", lambda *a: pytest.fail("denied source decrypted"))
    response = ask_secure(client, "검진은 기록 찾아줘", arm="bm25")
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "NO_RECORD"
    assert response.json()["citations"] == []
    assert record_id not in response.text
