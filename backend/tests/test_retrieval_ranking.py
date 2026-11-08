from __future__ import annotations

from dataclasses import asdict, dataclass

import pytest
from sqlalchemy import create_engine, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.auth_identity import AuthIdentity
from app.models.family import FamilyMember, FamilyRoom
from app.models.retrieval import RecordChunk, RecordEmbedding
from app.models.user import User
from app.models.processing_job import ProcessingJob
from app.models.upload import Upload, UploadFile
from app.retrieval.chunking import ChunkSource, canonical_json, chunk_processing_result
from app.retrieval.ranking import _bm25_tokens, fuse_rrf, rank_bm25, rank_substring
from app.retrieval.vector import cosine_similarity, rank_vector
from app.services import retrieval as retrieval_service
from app.services.retrieval import RetrievalSearchInvalid, search_room, search_room_lexical, vector_statement


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str


def _ids(results):
    return [getattr(result.chunk, "record_id", "record-" + result.chunk.id).removeprefix("record-") for result in results]


def _scores(results):
    return {getattr(result.chunk, "record_id", "record-" + result.chunk.id).removeprefix("record-"): result.score for result in results}


def test_bm25_matches_hand_calculated_scores_with_repeated_query_terms():
    # A wrong tf/df/avgdl/duplicate-query-term calculation changes these literals.
    results = rank_bm25(
        [
            Chunk("one", "alpha alpha beta"),
            Chunk("two", "alpha gamma"),
            Chunk("three", "beta gamma"),
        ],
        "alpha alpha beta",
    )

    assert _ids(results) == ["one", "two", "three"]
    assert _scores(results) == pytest.approx(
        {"one": 1.6171900773730123, "two": 0.9983525366047352, "three": 0.4991762683023676}
    )


def test_bm25_uses_unicode_tokens_and_excludes_empty_or_zero_match_results():
    chunks = [Chunk("korean", "가족, 추억! 가족"), Chunk("none", "다른 기록")]

    assert _ids(rank_bm25(chunks, "가족 추억")) == ["korean"]
    assert rank_bm25(chunks, "") == []
    assert rank_bm25([], "가족") == []
    assert rank_bm25(chunks, "없는말") == []


def test_bm25_tokenizer_nfkc_casefolds_and_splits_underscores():
    # Removing NFKC/casefold or treating _ as a word character changes these literal tokens.
    assert _bm25_tokens("ＦＯＯ_Bar 가족_기록") == ["foo", "bar", "가족", "기록"]


def test_substring_counts_only_non_overlapping_normalized_full_phrases():
    chunks = [
        Chunk("two", "가족\u3000여행 가족 여행"),
        Chunk("broken", "가족 오래 여행"),
        Chunk("one", "가족 여행"),
    ]

    substring = rank_substring(chunks, "가족 여행")
    bm25 = rank_bm25(chunks, "가족 여행")

    assert _ids(substring) == ["two", "one"]
    assert _scores(substring) == {"two": 2.0, "one": 1.0}
    assert "broken" not in _ids(substring)
    assert "broken" in _ids(bm25)
    assert rank_substring(chunks, "") == []


def test_equal_scores_use_chunk_id_order():
    chunks = [Chunk("z", "same"), Chunk("a", "same")]

    assert _ids(rank_substring(chunks, "same")) == ["a", "z"]
    assert _ids(rank_bm25(chunks, "same")) == ["a", "z"]


def _record_chunk(db, chunk_id: str, room_id: str, text: str) -> RecordChunk:
    from app.services.record_policy import register_legacy_record
    record_id, file_id, job_id = f"record-{chunk_id}", f"file-{chunk_id}", f"job-{chunk_id}"
    from ocr_approval_helpers import confirmed_review
    result = dict(text=text, extractor="AUTO_OCR", extractor_id="test")
    db.add(Upload(id=record_id, room_id=room_id, uploader_user_id="user",
                  type="image", title=chunk_id, status="uploaded"))
    db.flush()
    register_legacy_record(db, record_id)
    db.add(UploadFile(id=file_id, upload_id=record_id))
    db.add(ProcessingJob(id=job_id, record_id=record_id, upload_file_id=file_id,
                         room_id=room_id, processor="ocr", status="SUCCEEDED",
                         attempt=1, result_json=canonical_json(result), review_json=confirmed_review(result)))
    chunk = chunk_processing_result(ChunkSource(
        room_id=room_id, record_id=record_id, file_id=file_id,
        processing_job_id=job_id, source_attempt=1, result=result,
    ))[0]
    return RecordChunk(**asdict(chunk))


def test_room_local_service_does_not_use_another_rooms_documents_for_bm25():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        with sessions() as db:
            db.add_all(
                [
                    _record_chunk(db, "room-one", "room-1", "alpha alpha beta"),
                    _record_chunk(db, "room-two", "room-1", "alpha gamma"),
                    _record_chunk(db, "other", "room-2", "beta beta beta"),
                ]
            )
            db.commit()

        results = search_room_lexical(
            room_id="room-1", query="alpha beta", arm="bm25", k=50, session_factory=sessions
        )

        assert _ids(results) == ["room-one", "room-two"]
        assert _scores(results) == pytest.approx(
            {"room-one": 0.8780659561173048, "room-two": 0.19856803215183175}
        )
    finally:
        engine.dispose()


def _vector(*values: float) -> list[float]:
    return [*values, *([0.0] * (1024 - len(values)))]


def test_exact_cosine_rejects_invalid_vectors_and_sorts_stable_ties():
    # Removing finite/nonzero/dimension validation or the chunk-id tie break breaks this contract.
    chunks = [Chunk("z", "z"), Chunk("a", "a")]
    assert cosine_similarity(_vector(1), _vector(1)) == pytest.approx(1.0)
    assert _ids(rank_vector(zip(chunks, [_vector(1), _vector(1)]), _vector(1))) == ["a", "z"]
    for invalid in ([0.0] * 1024, _vector(float("nan")), _vector(float("inf")), [1.0]):
        with pytest.raises(ValueError):
            cosine_similarity(_vector(1), invalid)


def test_exact_cosine_handles_finite_high_magnitude_vectors_without_nan():
    # Unscaled dot/norm products overflow accepted finite values into NaN.
    assert cosine_similarity(_vector(1e308), _vector(1e308)) == pytest.approx(1.0)


def test_rrf_is_one_based_deduplicated_and_uses_zero_for_missing_arms():
    # Switching to zero-based ranks or averaging raw scores changes these hand-calculated totals.
    chunks = {identifier: Chunk(identifier, identifier) for identifier in ("a", "b", "c")}
    bm25 = [(chunks["a"], 9.0), (chunks["b"], 1.0)]
    vector = [(chunks["b"], 0.9), (chunks["a"], 0.8), (chunks["c"], 0.7)]

    results = fuse_rrf(bm25, vector)

    assert _ids(results) == ["a", "b", "c"]
    assert _scores(results) == pytest.approx(
        {"a": 1 / 61 + 1 / 62, "b": 1 / 62 + 1 / 61, "c": 1 / 63}
    )


def test_pgvector_statement_compiles_to_exact_cosine_ordering():
    # Dropping pgvector's comparator type makes PostgreSQL exact search fail before execution.
    sql = str(
        vector_statement(room_id="room-1", model_key="model-1", query_vector=_vector(1), k=5).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "<=>" in sql
    assert "ORDER BY distance, record_chunks.id" in sql
    assert "hnsw" not in sql.lower()
    assert "ivfflat" not in sql.lower()


def test_postgres_vector_arm_uses_requested_k_not_hybrid_candidate_cap(monkeypatch):
    # Passing candidate_k here limits the pure vector arm below the caller's requested result count.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        with sessions() as db:
            db.add_all(
                [
                    User(id="user", username="user", email="user@example.test", name="User"),
                    AuthIdentity(id="identity", user_id="user", provider="google", provider_subject="user"),
                    FamilyRoom(id="room", name="Room", invite_code="room-code", owner_user_id="user"),
                    FamilyMember(id="member", room_id="room", user_id="user"),
                ]
            )
            db.commit()

        requested = []

        def fake_statement(*, k, **_kwargs):
            requested.append(k)
            return select(*(getattr(RecordChunk, field) for field in retrieval_service._CHUNK_FIELDS), literal(0.0).label("distance")).where(False)

        class PostgresSession:
            def __init__(self):
                self._db = sessions()
                self.bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()

            def __getattr__(self, name):
                return getattr(self._db, name)

        class Provider:
            model_key = "model"
            dimensions = 1024

            def embed(self, texts):
                return [_vector(1)]

        monkeypatch.setattr(retrieval_service, "vector_statement", fake_statement)
        assert search_room(room_id="room", user_id="user", query="needle", arm="vector", k=5,
                           provider=Provider(), session_factory=PostgresSession, candidate_limit=1) == []
        assert requested == [5]
    finally:
        engine.dispose()


def test_vector_and_rrf_keep_other_models_rooms_and_missing_embeddings_out():
    # Removing either room/model filter, or letting unembedded chunks into RRF, changes these IDs.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        with sessions() as db:
            db.add_all(
                [
                    User(id="user", username="user", email="user@example.test", name="User"),
                    AuthIdentity(id="identity", user_id="user", provider="google", provider_subject="user"),
                    FamilyRoom(id="room", name="Room", invite_code="room-code", owner_user_id="user"),
                    FamilyRoom(id="other-room", name="Other", invite_code="other-code", owner_user_id="user"),
                    FamilyMember(id="member", room_id="room", user_id="user"),
                ]
            )
            rows = [
                _record_chunk(db, "own", "room", "needle"),
                _record_chunk(db, "wrong-model", "room", "needle needle"),
                _record_chunk(db, "missing", "room", "needle needle needle"),
                _record_chunk(db, "other-room", "other-room", "needle needle needle needle"),
            ]
            db.add_all(rows)
            db.add_all(
                [
                    RecordEmbedding(id="own-embedding", chunk_id=rows[0].id, model_key="model", dimensions=1024, embedding=_vector(1)),
                    RecordEmbedding(id="wrong-embedding", chunk_id=rows[1].id, model_key="other-model", dimensions=1024, embedding=_vector(1)),
                    RecordEmbedding(id="other-embedding", chunk_id=rows[3].id, model_key="model", dimensions=1024, embedding=_vector(1)),
                ]
            )
            db.commit()

        class Provider:
            model_key = "model"
            dimensions = 1024

            def embed(self, texts):
                return [_vector(1)]

        for arm in ("vector", "hybrid_rrf"):
            assert _ids(search_room(room_id="room", user_id="user", query="needle", arm=arm, k=5,
                                    provider=Provider(), session_factory=sessions, candidate_limit=200)) == ["own"]
    finally:
        engine.dispose()


def test_vector_empty_corpus_and_invalid_hybrid_candidate_limit_are_not_empty_success():
    # A zero candidate limit must be a typed configuration error, not an indistinguishable empty result.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        with sessions() as db:
            db.add_all(
                [
                    User(id="user", username="user", email="user@example.test", name="User"),
                    AuthIdentity(id="identity", user_id="user", provider="google", provider_subject="user"),
                    FamilyRoom(id="room", name="Room", invite_code="room-code", owner_user_id="user"),
                    FamilyMember(id="member", room_id="room", user_id="user"),
                ]
            )
            db.commit()

        class Provider:
            model_key = "model"
            dimensions = 1024

            def embed(self, texts):
                return [_vector(1)]

        assert search_room(room_id="room", user_id="user", query="needle", arm="vector", k=1,
                           provider=Provider(), session_factory=sessions, candidate_limit=200) == []
        with pytest.raises(RetrievalSearchInvalid):
            search_room(room_id="room", user_id="user", query="needle", arm="hybrid_rrf", k=1,
                        provider=Provider(), session_factory=sessions, candidate_limit=0)
    finally:
        engine.dispose()


def test_sqlite_hybrid_rrf_excludes_vector_rank_after_candidate_cap():
    # Passing the whole SQLite vector scan into fusion gives target an illegal rank-21 contribution.
    # Return all tied results: generated chunk UUIDs no longer force target to sort first.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        with sessions() as db:
            db.add_all(
                [
                    User(id="user", username="user", email="user@example.test", name="User"),
                    AuthIdentity(id="identity", user_id="user", provider="google", provider_subject="user"),
                    FamilyRoom(id="room", name="Room", invite_code="room-code", owner_user_id="user"),
                    FamilyMember(id="member", room_id="room", user_id="user"),
                ]
            )
            rows = [_record_chunk(db, "a-target", "room", "needle")]
            rows.extend(_record_chunk(db, f"other-{index:02}", "room", "unrelated") for index in range(20))
            db.add_all(rows)
            db.add_all(
                [
                    RecordEmbedding(id=f"embedding-{row.id}", chunk_id=row.id, model_key="model", dimensions=1024,
                                    embedding=_vector(1, 100) if row.record_id == "record-a-target" else _vector(1, 1))
                    for row in rows
                ]
            )
            db.commit()

        class Provider:
            model_key = "model"
            dimensions = 1024

            def embed(self, texts):
                return [_vector(1)]

        scores = _scores(search_room(room_id="room", user_id="user", query="needle", arm="hybrid_rrf", k=21,
                                     provider=Provider(), session_factory=sessions, candidate_limit=20))
        assert scores["a-target"] == pytest.approx(1 / 61)
    finally:
        engine.dispose()
