import pytest

from test_grounded_answering_api import rig, set_result, index, add_safety_sources, ask

MATCH = "아버지 검진 오전 9시"
HIDDEN = "아버지의 검진은 오후 2시"
QUERY = "아버지 검진 몇 시?"


@pytest.mark.parametrize("arm", ["bm25", "vector", "hybrid_rrf"])
@pytest.mark.parametrize("count,conflict,expected", [
    (51, True, "CLARIFY"), (51, False, "CLARIFY"),
    (2, True, "CONFLICT"), (2, False, "ANSWER"),
])
def test_safety_universe_precedes_ranking(rig, arm, count, conflict, expected):
    tracker, embedding, provider, client = rig
    set_result(tracker, MATCH)
    index(tracker, embedding)
    sources = [(f"job-hidden-{number}", MATCH) for number in range(count - 1)]
    if conflict:
        sources[-1] = (sources[-1][0], HIDDEN)
    add_safety_sources(tracker, embedding, sources)
    if conflict and arm in {"vector", "hybrid_rrf"}:
        from app.models.retrieval import RecordChunk, RecordEmbedding
        with tracker.session_maker() as db:
            ids = db.query(RecordChunk.id).filter(RecordChunk.text == HIDDEN)
            db.query(RecordEmbedding).filter(RecordEmbedding.chunk_id.in_(ids)).delete(synchronize_session=False)
            db.commit()
    response = ask(client, QUERY, arm=arm)
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == expected
    assert provider.calls == (1 if expected == "ANSWER" else 0)
    if expected != "ANSWER":
        assert provider.factory_calls == []
        assert response.json()["provider_mode"] == "not_called"


@pytest.mark.parametrize("arm", ["bm25", "vector", "hybrid_rrf"])
@pytest.mark.parametrize("count,conflict,expected", [
    (51, True, "CLARIFY"), (51, False, "CLARIFY"),
    (2, True, "CONFLICT"), (2, False, "ANSWER"),
])
def test_secure_safety_universe_precedes_ranking(monkeypatch, arm, count, conflict, expected):
    from types import SimpleNamespace
    from app.ai.grounded_answering import load_secure_grounded_snapshot, generate_grounded_answer
    from app.retrieval.chunking import ChunkSource, chunk_processing_result
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    from app.schemas.ai import AskRequest
    from app.services import secure_retrieval
    from test_retrieval_indexing import result_payload

    chunks = tuple(chunk_processing_result(ChunkSource(
        room_id="room", record_id=f"record-{n}", file_id=f"file-{n}",
        processing_job_id=f"job-{n}", source_attempt=1,
        result=result_payload(HIDDEN if conflict and n == count - 1 else MATCH),
    ))[0] for n in range(count))
    embedding = SyntheticTokenHashProvider()
    corpus = SimpleNamespace(
        chunks=chunks, member_names=(), record_texts=(),
        model_key=embedding.model_key,
        vectors=tuple((c, v) for c, v in zip(chunks, embedding.embed([c.text for c in chunks]))
                      if c.text != HIDDEN),
    )
    monkeypatch.setattr(secure_retrieval, "materialize_room", lambda *a, **kw: corpus)
    monkeypatch.setattr(secure_retrieval, "revalidate_evidence", lambda *a: None)
    snapshot = load_secure_grounded_snapshot(
        runtime=SimpleNamespace(embedding_provider=embedding),
        user_id="user", room_id="room", candidate_limit=200,
        payload=AskRequest(query=QUERY, arm=arm, k=1, model_id="exaone-45-33b-dgx"),
    )
    assert snapshot.route == expected
    if expected != "ANSWER":
        result = generate_grounded_answer(
            snapshot, provider_factory=lambda _: pytest.fail("inference called"),
        )
        assert result.provider_mode == "not_called"



def test_no_ranked_matches_does_not_create_empty_answer_plan(rig):
    tracker, embedding, provider, client = rig
    set_result(tracker, HIDDEN)
    index(tracker, embedding)
    response = ask(client, QUERY, arm="bm25")
    assert response.status_code == 200, response.text
    assert response.json()["gate_route"] == "NO_RECORD"
    assert provider.calls == 0
