"""Paired retrieval and current MVP answering through actual guarded HTTP routes."""
import json
from ocr_approval_helpers import confirmed_review
from dataclasses import replace

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine
from test_secure_catalogue import catalogue as catalogue
from test_secure_identity import identity_client as identity_client, create_room
from test_secure_media import media_client as media_fixture, upload, job
from app.core.database import Base
from app.models.user import User
from app.models.auth_identity import AuthIdentity
from app.models.family import FamilyRoom, FamilyMember
from app.models.upload import Upload, UploadFile
from app.models.processing_job import ProcessingJob
from app.processing.synthetic import SyntheticOCRProvider
from app.processing.contracts import OCRResult
from app.retrieval.evaluation import SyntheticTokenHashProvider, query_metrics, evaluate_rankings
from app.services.retrieval import search_room
from app.services.retrieval_indexing import index_processing_result
from app.services.record_policy import register_legacy_record
from app.ai.providers.base import InferenceRequest, InferenceResponse

QUOTE = "아버지 검진은 오후 2시였다."
ARMS = ("substring", "bm25", "vector", "hybrid_rrf")
RECORD_ANSWER = "합성 서술 응답입니다."
media_client = media_fixture


@pytest.fixture
def record_answer_provider(monkeypatch: pytest.MonkeyPatch) -> list[InferenceRequest]:
    from app.ai.grounded_answering import SyntheticPlanProvider

    requests: list[InferenceRequest] = []

    def generate(self: SyntheticPlanProvider, request: InferenceRequest) -> InferenceResponse:
        assert request.prompt_package["response_mode"] == "record_answer"
        requests.append(request)
        return InferenceResponse("synthetic-record", request.model_id, RECORD_ANSWER, "synthetic")

    monkeypatch.setattr(SyntheticPlanProvider, "generate", generate)
    return requests


def processed(c, monkeypatch, quote=QUOTE, *, approved=True):
    async def extract(self, content, mime):
        return OCRResult(quote, 1.0, "ko")
    monkeypatch.setattr(SyntheticOCRProvider, "extract", extract)
    c.app.state.secure_runtime.processing_providers = {"ocr": SyntheticOCRProvider()}
    from app.ai.grounded_answering import SyntheticPlanProvider
    c.app.state.secure_runtime.inference_provider = SyntheticPlanProvider()
    row = upload(c)
    queued = job(c, row)
    response = c.request("POST", "/processing-jobs/" + queued["job_id"] + "/run", headers=c.auth)
    assert response.status_code == 200 and response.json()["status"] == "SUCCEEDED", response.text
    if approved:
        approval = c.request("POST", "/processing-jobs/" + queued["job_id"] + "/review",
                             headers=c.auth, json={"action": "approve"})
        assert approval.status_code == 200, approval.text
    return row, response.json()


def index_secure(c, row, done):
    return c.request("POST", "/records/" + row["upload_id"] + "/retrieval-index", headers=c.auth,
                     json={"processing_job_id": done["job_id"]})


def search_secure(c, arm="bm25", query="아버지 검진", k=5):
    return c.request("POST", "/families/" + c.room["room_id"] + "/retrieval/search", headers=c.auth,
                     json=dict(query=query, arm=arm, k=k))


def ask_secure(c, query="아버지 검진은 몇 시였어?", arm="vector", k=1):
    return c.request("POST", "/families/" + c.room["room_id"] + "/ask", headers=c.auth,
                     json=dict(query=query, model_id="exaone-45-33b-dgx", arm=arm, k=k))


def test_secure_ask_uses_51st_candidate_as_limit_sentinel(monkeypatch):
    from types import SimpleNamespace

    from app.ai.grounded_answering import generate_grounded_answer, load_secure_grounded_snapshot
    from app.retrieval.chunking import RetrievalChunk, sha256_text
    from typing import cast
    from app.retrieval.ranking import RankedChunk, ChunkLike
    from app.schemas.ai import AskRequest
    from app.services import retrieval

    def candidate(number):
        text_value = QUOTE
        return RetrievalChunk(
            id=f"candidate-{number:03d}", room_id="room-1", record_id=f"record-{number:03d}",
            file_id=f"file-{number:03d}", processing_job_id=f"job-{number:03d}", source_attempt=1,
            source_result_sha256="a" * 64, ordinal=0, text=text_value,
            text_sha256=sha256_text(text_value), token_count=4, char_start=0,
            char_end=len(text_value), source_locator_json='{"kind":"full_text"}',
            extractor="AUTO_OCR", extractor_id="ocr-v1", review_status="unreviewed",
            user_confirmed=False, chunker_version="ws160-o32-v1",
        )

    observed = {}
    corpus = SimpleNamespace(member_names=(), record_texts=(), chunks=tuple(candidate(n) for n in range(51)))
    def search(**kwargs):
        observed.update(kwargs)
        return [RankedChunk(cast(ChunkLike, candidate(number)), 1, bm25=1) for number in range(51)], corpus
    monkeypatch.setattr(retrieval, "search_secure_room", search)

    snapshot = load_secure_grounded_snapshot(
        runtime=object(), user_id="user-1", room_id="room-1",
        payload=AskRequest(query="아버지 검진은 몇 시였어?", model_id="exaone-45-33b-dgx",
                           arm="bm25", k=1),
        candidate_limit=200,
    )

    assert observed["k"] == 51
    assert snapshot.route == "CLARIFY"
    assert snapshot.safety_reason == "SAFETY_CANDIDATE_LIMIT_EXCEEDED"
    result = generate_grounded_answer(
        snapshot, provider_factory=lambda _model_id: pytest.fail("safety-limit inference called"),
    )
    assert result.provider_mode == "not_called"


@pytest.fixture
def retrieval_pair(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    rows = [processed(c, monkeypatch, quote) for quote in
            (QUOTE, "어머니 산책은 3번이었다.", "제주도 여행에서 바다를 보았다.")]
    engine = fk_enforced_engine()
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    uid, rid = c.session["user"]["user_id"], c.room["room_id"]
    with sessions() as db:
        db.add(User(id=uid, username="paired", name="Synthetic Person", email="paired@example.test"))
        db.flush()
        db.add(AuthIdentity(id=uid, user_id=uid, provider="local", provider_subject="paired", credential_hash="synthetic"))
        db.add(FamilyRoom(id=rid, name="paired", invite_code="paired", owner_user_id=uid))
        db.flush()
        db.add(FamilyMember(id=rid, user_id=uid, room_id=rid, role="owner"))
        for row, done in rows:
            db.add(Upload(id=row["upload_id"], room_id=rid, uploader_user_id=uid, type="image", title=row["title"], description=row["description"], status="uploaded"))
            db.flush()
            register_legacy_record(db, row["upload_id"])
            db.add(UploadFile(id=row["file_id"], upload_id=row["upload_id"], storage_bucket="synthetic", storage_path="synthetic.png", mime_type="image/png"))
            db.flush()
            db.add(ProcessingJob(id=done["job_id"], record_id=row["upload_id"], room_id=rid, upload_file_id=row["file_id"], processor="ocr", status="SUCCEEDED", attempt=done["attempt"], result_json=json.dumps(done["result"], ensure_ascii=False), review_json=confirmed_review(done["result"], done["attempt"])))
        db.commit()
    c.legacy_sessions, c.rows = sessions, rows
    try:
        yield c
    finally:
        engine.dispose()


@pytest.mark.parametrize("arm", ARMS)
def test_secure_ranking_preserves_exact_ids_ranks_quotes_and_metrics(retrieval_pair, arm):
    c = retrieval_pair
    provider = c.app.state.secure_runtime.embedding_provider
    for row, done in c.rows:
        expected = index_processing_result(record_id=row["upload_id"], processing_job_id=done["job_id"],
            user_id=c.session["user"]["user_id"], provider=provider, session_factory=c.legacy_sessions)
        actual = index_secure(c, row, done)
        assert actual.status_code == 200, actual.text
        assert actual.json() == expected.model_dump()
    actual_eval, legacy_eval = {}, {}
    for query, relevant_index in (("아버지 검진", 0), ("산책", 1), ("제주도 여행", 2), ("없는말", None)):
        for k in (1, 3, 5, 10):
            expected = search_room(room_id=c.room["room_id"], user_id=c.session["user"]["user_id"], query=query,
                arm=arm, k=k, provider=provider, session_factory=c.legacy_sessions, candidate_limit=100)
            response = search_secure(c, arm, query, k)
            assert response.status_code == 200, response.text
            hits = response.json()["hits"]
            assert [(h["chunk_id"], h["record_id"], h["file_id"], h["text"], h["source_locator"], h["rank"]) for h in hits] == [
                (h.chunk.id, h.chunk.record_id, h.chunk.file_id, h.chunk.text, json.loads(h.chunk.source_locator_json), n) for n, h in enumerate(expected, 1)]
            assert [h["scores"][arm] for h in hits] == pytest.approx([h.score for h in expected])
            relevant = {c.rows[relevant_index][0]["upload_id"]} if relevant_index is not None else set()
            a, b = [h["record_id"] for h in hits], [h.chunk.record_id for h in expected]
            assert query_metrics(a, relevant, k) == query_metrics(b, relevant, k)
            actual_eval[query + str(k)] = dict(ranked_record_ids=a, relevant_record_ids=relevant)
            legacy_eval[query + str(k)] = dict(ranked_record_ids=b, relevant_record_ids=relevant)
    assert evaluate_rankings(actual_eval) == evaluate_rankings(legacy_eval)
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM record_chunks")) == 0
        assert db.scalar(text("SELECT count(*) FROM record_embeddings")) == 0
        assert db.scalar(text("SELECT count(*) FROM processing_jobs WHERE result_json IS NOT NULL")) == 0
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE artifact_kind='room_derivatives' AND state='ACTIVE'")) == 3


def test_first_attempt_secure_citation_contract_and_hashless_legacy_refusal(retrieval_pair, record_answer_provider):
    import hashlib
    from uuid import UUID, uuid5
    from app.ai.grounded_answering import (
        GroundedEvidenceChanged, load_grounded_answer_snapshot, build_fact_sheet,
    )
    from app.schemas.ai import AskRequest

    c = retrieval_pair
    row, done = c.rows[0]
    assert done["attempt"] == 0 and "source_sha256" not in done["result"]
    provider = c.app.state.secure_runtime.embedding_provider
    index_processing_result(record_id=row["upload_id"], processing_job_id=done["job_id"],
        user_id=c.session["user"]["user_id"], provider=provider, session_factory=c.legacy_sessions)
    assert index_secure(c, row, done).status_code == 200
    # The cloned historical result lacks the new LEGACY provider-input digest.
    with pytest.raises(GroundedEvidenceChanged):
        load_grounded_answer_snapshot(session_factory=c.legacy_sessions, room_id=c.room["room_id"],
            user_id=c.session["user"]["user_id"],
            payload=AskRequest(query="아버지 검진은 몇 시였어?", model_id="exaone-45-33b-dgx", arm="vector", k=1),
            retrieval_provider=provider, candidate_limit=100)

    chunk = search_room(room_id=c.room["room_id"], user_id=c.session["user"]["user_id"],
        query="아버지", arm="bm25", k=1, provider=provider,
        session_factory=c.legacy_sessions, candidate_limit=100)[0].chunk
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    source = {
        "room_id": c.room["room_id"], "chunk_id": chunk.id, "record_id": row["upload_id"],
        "file_id": row["file_id"], "processing_job_id": done["job_id"], "source_attempt": 0,
        "source_result_sha256": hashlib.sha256(canonical(done["result"]).encode()).hexdigest(),
        "text_sha256": hashlib.sha256(QUOTE.encode()).hexdigest(),
        "chunk_char_start": 0, "chunk_char_end": len(QUOTE),
        "source_locator_json": '{"kind":"full_text"}', "extractor": "AUTO_OCR",
        "extractor_id": "synthetic-ocr-v1", "review_status": "unreviewed", "user_confirmed": False,
    }
    expected_fact_id = str(uuid5(UUID("f8f41d4d-4db1-4b13-a9a6-f55bcb651ee2"), canonical({
        "source": source, "quote_char_start": 0, "quote_char_end": len(QUOTE),
    })))
    actual = ask_secure(c)
    assert actual.status_code == 200, actual.text
    # SECURE immutable identity extends the unchanged exact fact identity and spans.
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(row["file_url"]).query)
    with c.engine.connect() as db:
        epoch = db.scalar(text("SELECT authorization_epoch FROM records WHERE id=:r"), {"r": row["upload_id"]})
    body = actual.json()
    assert body["gate_route"] == "ANSWER" and body["answer"] == RECORD_ANSWER
    assert body["gate_action"] == "llm_generated" and body["provider_mode"] == "local"
    assert len(record_answer_provider) == 1
    assert QUOTE in "\n".join(record_answer_provider[0].prompt_package["retrieved_evidence"])
    assert body["query_context"]["scope"] == "ROOM"
    assert [record["record_id"] for record in body["query_context"]["records"]] == [row["upload_id"]]
    assert body["citations"] == [{
            "fact_id": expected_fact_id, "chunk_id": chunk.id, "record_id": row["upload_id"],
            "file_id": row["file_id"], "chunk_char_start": 0, "chunk_char_end": len(QUOTE),
            "source_locator": {"kind": "full_text"}, "extractor": "AUTO_OCR",
            "extractor_id": "synthetic-ocr-v1", "review_status": "unreviewed",
            "user_confirmed": False, "evidence_status": "extracted",
            "source_object_id": query["source_object_id"][0],
            "source_object_version": int(query["source_object_version"][0]),
            "source_ciphertext_sha256": query["expected_ciphertext_sha256"][0],
            "original_url": urlsplit(row["file_url"]).path + "?" + urlsplit(row["file_url"]).query
                + "&processing_job_id=" + done["job_id"] + "&source_attempt=0&source_result_sha256=" + source["source_result_sha256"]
                + "&authorization_epoch=" + str(epoch),
        }]
    with pytest.raises(ValueError):
        build_fact_sheet([replace(chunk, source_attempt=-1)])


@pytest.mark.parametrize("race", ["record", "source", "attempt", "membership"])
def test_index_provider_races_do_not_publish(media_client, monkeypatch, race):
    c = media_client
    row, done = processed(c, monkeypatch)
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    original = SyntheticTokenHashProvider.embed
    def mutate(self, texts):
        with c.engine.begin() as db:
            if race in ("record", "source"):
                db.execute(text("UPDATE protected_pointers SET revision=revision+1,source_revision=source_revision+1 WHERE owner_type=:k"), dict(k="record" if race == "record" else "file"))
            elif race == "attempt":
                db.execute(text("UPDATE processing_jobs SET attempt=attempt+1"))
            else:
                from app.models.secure_catalogue import catalogue_mutation
                mid = db.scalar(text("SELECT id FROM family_members WHERE room_id=:r"), dict(r=c.room["room_id"]))
                with catalogue_mutation(db, "membership", "family_members", [mid]):
                    db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room'"))
                    db.execute(text("UPDATE family_members SET is_active=false WHERE id=:m"), dict(m=mid))
        return original(self, texts)
    monkeypatch.setattr(SyntheticTokenHashProvider, "embed", mutate)
    response = index_secure(c, row, done)
    if race == "membership":
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": {"code": "RESOURCE_NOT_AVAILABLE"}}
    else:
        assert response.status_code in (409, 503), response.text
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE artifact_kind='room_derivatives' AND state='ACTIVE'")) == 0
        if race == "membership":
            assert db.scalar(text("SELECT count(*) FROM family_members WHERE room_id=:r AND is_active=true"), dict(r=c.room["room_id"])) == 0


def test_full_room_count_limit_before_top_k(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, "word " * (128 * 1000 + 33))
    response = index_secure(c, row, done)
    assert response.status_code == 413, response.text


def test_forbidden_embedding_is_not_called(media_client, monkeypatch):
    c = media_client
    row, done = processed(c, monkeypatch)
    class Remote:
        model_key, dimensions = "remote", 1024
        def embed(self, values):
            pytest.fail("real provider called")
    c.app.state.secure_runtime.embedding_provider = Remote()
    response = index_secure(c, row, done)
    assert response.status_code == 503
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE owner_type='record_index'")) == 0


def test_negative_attempt_rejected_and_initial_attempt_zero_is_literal(retrieval_pair):
    from app.ai.grounded_answering import build_fact_sheet
    from app.retrieval.chunking import ChunkSource, chunk_processing_result
    c = retrieval_pair
    row, done = c.rows[0]
    chunk = chunk_processing_result(ChunkSource(c.room["room_id"], row["upload_id"], row["file_id"], done["job_id"], 0, done["result"]))[0]
    assert build_fact_sheet([chunk]).facts[0].quote == QUOTE
    with pytest.raises(ValueError):
        build_fact_sheet([replace(chunk, source_attempt=-1)])


def rewrite_derivative(c, mutate):
    """Valid AEAD/publication with deliberately inconsistent synthetic provenance."""
    from uuid import UUID
    from app.services.secure_retrieval import materialize_room
    from app.api.routes.secure_media import stage
    from app.api.routes.secure_operations import raw_json
    from app.services import secure_catalogue as cat
    uid, runtime = c.session["user"]["user_id"], c.app.state.secure_runtime
    corpus = materialize_room(runtime, uid, c.room["room_id"])
    oid, doc, _ = corpus.documents[0]
    mutate(doc)
    target = ("record_index", UUID(oid))
    record = next(s for s in corpus.records if s[1]["id"] == oid)
    staged = stage(runtime, uid, target, raw_json(doc), dependencies=tuple(p[:2] for p in corpus.catalogue.pointers if p[:2] != target),
        source=(record[2][0].object_id, record[2][0].object_version))
    with c.engine.begin() as db:
        cat.publish_many(db, (staged,))


@pytest.mark.parametrize("damage", ["hash", "quote", "locator", "attempt", "dimensions", "result_object"])
def test_authenticated_derivatives_require_exact_source_proof(media_client, monkeypatch, damage):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    def mutate(doc):
        entry = doc["jobs"][0]
        if damage == "hash":
            entry["source_result_sha256"] = "0" * 64
        elif damage == "quote":
            entry["chunks"][0]["text"] = "fabricated private quote"
        elif damage == "locator":
            entry["chunks"][0]["source_locator_json"] = '{"kind":"full_text","storage_path":"private"}'
        elif damage == "attempt":
            entry["attempt"] += 1
        elif damage == "dimensions":
            doc["dimensions"] = 768
        else:
            entry["result_object_id"] = row["upload_id"]
    rewrite_derivative(c, mutate)
    for response in (search_secure(c), ask_secure(c)):
        assert response.status_code in (409, 503), response.text
        assert QUOTE not in response.text and "fabricated" not in response.text


@pytest.mark.parametrize("race", ["attempt", "record", "source", "member", "ciphertext"])
def test_answer_revalidates_after_inert_inference(media_client, monkeypatch, race):
    from app.ai.grounded_answering import SyntheticPlanProvider
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    original = SyntheticPlanProvider.generate
    def generate(self, request):
        with c.engine.begin() as db:
            if race == "attempt":
                db.execute(text("UPDATE processing_jobs SET attempt=attempt+1"))
            elif race in ("record", "source"):
                db.execute(text("UPDATE protected_pointers SET revision=revision+1,source_revision=source_revision+1 WHERE owner_type=:k"), dict(k="record" if race == "record" else "file"))
            elif race == "member":
                from app.models.secure_catalogue import catalogue_mutation
                mid = db.scalar(text("SELECT id FROM family_members WHERE room_id=:r"), dict(r=c.room["room_id"]))
                with catalogue_mutation(db, "membership", "family_members", [mid]):
                    db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room'"))
                    db.execute(text("UPDATE family_members SET is_active=false WHERE id=:m"), dict(m=mid))
            else:
                oid = db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='job'"))
                receipt, blob = c.objects[oid]
                c.objects[oid] = receipt, blob[:-1] + bytes([blob[-1] ^ 1])
        return original(self, request)
    monkeypatch.setattr(SyntheticPlanProvider, "generate", generate)
    c.app.state.secure_runtime.inference_provider = SyntheticPlanProvider()
    response = ask_secure(c)
    if race == "member":
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": {"code": "RESOURCE_NOT_AVAILABLE"}}
    else:
        assert response.status_code in (409, 503), response.text
    assert QUOTE not in response.text and "citations" not in response.text
    if race == "member":
        with c.engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM family_members WHERE room_id=:r AND is_active=true"), dict(r=c.room["room_id"])) == 0


def test_replacement_invalidates_index_and_new_source_reindexes(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    upload(c, row)
    assert search_secure(c).json()["hits"] == []
    path = "/processing-jobs/" + done["job_id"]
    assert c.request("POST", path + "/retry", headers=c.auth).status_code == 200
    completed = c.request("POST", path + "/run", headers=c.auth)
    assert completed.status_code == 200 and completed.json()["attempt"] == 2
    assert index_secure(c, row, completed.json()).status_code in (409, 503)
    assert c.request("POST", path + "/review", headers=c.auth, json={"action": "approve"}).status_code == 200
    assert index_secure(c, row, completed.json()).status_code == 200
    assert search_secure(c).json()["hits"][0]["text"] == QUOTE


def test_room_model_mismatch_and_query_no_matching_embeddings(media_client, monkeypatch):
    c = media_client
    provider = SyntheticTokenHashProvider()
    c.app.state.secure_runtime.embedding_provider = provider
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    provider.model_key = "synthetic-second-space"
    for arm in ("vector", "hybrid_rrf"):
        assert search_secure(c, arm).json()["hits"] == []
    assert search_secure(c, "bm25").json()["hits"][0]["text"] == QUOTE
    assert index_secure(c, row, done).status_code == 409
    provider.dimensions = 768
    assert search_secure(c, "vector").status_code == 503


def test_aggregate_ciphertext_capacity_checked_before_crypto(retrieval_pair, monkeypatch):
    c = retrieval_pair
    for row, done in c.rows[:2]:
        assert index_secure(c, row, done).status_code == 200
    with c.engine.begin() as db:
        db.execute(text("UPDATE encrypted_objects SET ciphertext_size=:n WHERE artifact_kind='room_derivatives' AND state='ACTIVE'"), dict(n=17 * 1024 * 1024))
    response = search_secure(c, k=1)
    assert response.status_code == 413, response.text


def test_ciphertext_framing_counts_at_publication(media_client, monkeypatch):
    from app.services import secure_retrieval as secure
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    with c.engine.connect() as db:
        size = db.scalar(text("SELECT ciphertext_size FROM encrypted_objects WHERE artifact_kind='room_derivatives' AND state='ACTIVE'"))
    monkeypatch.setattr(secure, "MAX_CIPHERTEXT", size)
    assert index_secure(c, row, done).status_code == 200
    # Replacement fits plaintext but exceeds the complete framing limit by one byte.
    upload(c, row)
    path = "/processing-jobs/" + done["job_id"]
    assert c.request("POST", path + "/retry", headers=c.auth).status_code == 200
    completed = c.request("POST", path + "/run", headers=c.auth).json()
    assert c.request("POST", path + "/review", headers=c.auth, json={"action": "approve"}).status_code == 200
    monkeypatch.setattr(secure, "MAX_CIPHERTEXT", size - 1)
    assert index_secure(c, row, completed).status_code == 413
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE owner_type='record_index' AND state='ABANDONED'")) == 1


def test_thousand_chunk_corpus_and_no_plaintext_sql_during_search(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch, "word " * (128 * 999 + 160))
    response = index_secure(c, row, done)
    assert response.status_code == 200 and response.json()["chunk_count"] == 1000, response.text
    def forbid_plain(connection, cursor, statement, params, context, executemany):
        if "record_chunks" in statement.lower() or "record_embeddings" in statement.lower():
            pytest.fail("secure search read plaintext retrieval tables")
    event.listen(c.engine, "before_cursor_execute", forbid_plain)
    for arm in ARMS:
        response = search_secure(c, arm, "word", 1)
        assert response.status_code == 200 and len(response.json()["hits"]) == 1, response.text


def test_provider_and_send_sql_boundaries_and_forbidden_inference(media_client, monkeypatch, record_answer_provider):
    from app.ai.grounded_answering import SyntheticPlanProvider
    c = media_client
    checked = set()
    event.listen(c.engine, "checkout", lambda conn, rec, proxy: checked.add(id(conn)))
    event.listen(c.engine, "checkin", lambda conn, rec: checked.discard(id(conn)))
    original_embed, original_generate = SyntheticTokenHashProvider.embed, SyntheticPlanProvider.generate
    def embed(self, values):
        assert not checked and c.app.state.secure_runtime.lock.held
        return original_embed(self, values)
    def generate(self, request):
        assert not checked and c.app.state.secure_runtime.lock.held
        return original_generate(self, request)
    monkeypatch.setattr(SyntheticTokenHashProvider, "embed", embed)
    monkeypatch.setattr(SyntheticPlanProvider, "generate", generate)
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    c.app.state.secure_runtime.inference_provider = SyntheticPlanProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    response = ask_secure(c)
    assert response.status_code == 200 and response.json()["provider_mode"] == "local", response.text
    assert response.json()["answer"] == RECORD_ANSWER and len(record_answer_provider) == 1
    class Remote:
        def generate(self, request):
            pytest.fail("real inference called")
    c.app.state.secure_runtime.inference_provider = Remote()
    assert ask_secure(c).status_code == 503
    assert len(record_answer_provider) == 1


@pytest.mark.parametrize("query,quotes,route", [
    pytest.param("부산 여행의 정확한 날짜는?", [QUOTE], "ANSWER", id="weak-topic-candidates"),
    pytest.param("할머니는 몇 시였어?", [QUOTE], "ANSWER", id="unresolved-person-candidates"),
    pytest.param("아버지 검진은 몇 시였어?", [QUOTE, "아버지 검진은 오전 9시였다."], "ANSWER", id="conflicting-candidates"),
    pytest.param("아버지 검진은 몇 시였어?", [""], "NO_RECORD", id="empty-evidence"),
])
def test_mvp_candidates_reach_model_and_all_routes_revalidate(media_client, monkeypatch, record_answer_provider, query, quotes, route):
    from app.ai import record_questions
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    record_ids = []
    for quote in quotes:
        row, done = processed(c, monkeypatch, quote)
        assert index_secure(c, row, done).status_code == 200
        record_ids.append(row["upload_id"])
    response = ask_secure(c, query, k=2)
    assert response.status_code == 200 and response.json()["gate_route"] == route, response.text
    body = response.json()
    if any(quotes):
        assert body["answer"] == RECORD_ANSWER and body["provider_mode"] == "local"
        assert body["gate_action"] == "llm_generated" and len(record_answer_provider) == 1
        assert {citation["record_id"] for citation in body["citations"]} == set(record_ids)
        evidence = "\n".join(record_answer_provider[0].prompt_package["retrieved_evidence"])
        assert all(quote in evidence for quote in quotes)
        assert record_answer_provider[0].user_query == query
    else:
        assert body["citations"] == [] and body["provider_mode"] == "not_called"
        assert body["gate_action"] == "llm_not_called" and not record_answer_provider
        assert body["query_context"]["records"] == []
    original = record_questions.load_record_question
    mutations = []

    def change(**kwargs):
        snapshot = original(**kwargs)
        with c.engine.begin() as db:
            db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room'"))
        mutations.append(True)
        return snapshot

    monkeypatch.setattr(record_questions, "load_record_question", change)
    stale = ask_secure(c, query, k=2)
    assert mutations == [True]
    assert stale.status_code in (409, 503), stale.text
    assert "citations" not in stale.json() and RECORD_ANSWER not in stale.text and QUOTE not in stale.text


def test_actual_outer_search_holds_guard_through_send_and_reports_stage_timings(media_client, monkeypatch):
    import asyncio
    from test_secure_media import raw_scope
    c = media_client
    runtime = c.app.state.secure_runtime
    runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    async def run():
        entered, release, contender_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        scope = raw_scope(c, "POST", "/families/" + c.room["room_id"] + "/retrieval/search", [(b"content-type", b"application/json")])
        scope["secure_timings"] = timings = {}
        supplied = False
        messages = []
        async def receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return dict(type="http.request", more_body=False, body=json.dumps(dict(query="검진", arm="vector", k=1)).encode())
            await asyncio.Future()
        async def send(message):
            assert runtime.lock.held
            messages.append(message)
            if message["type"] == "http.response.body":
                entered.set()
                await release.wait()
        async def contender():
            async with runtime.operation_guard():
                contender_entered.set()
        task = asyncio.create_task(c.app(scope, receive, send))
        await asyncio.wait_for(entered.wait(), 3)
        queued = asyncio.create_task(contender())
        await asyncio.sleep(.01)
        assert not contender_entered.is_set()
        release.set()
        await task
        await queued
        assert messages[0]["status"] == 200
        assert set(("queue_wait_ns", "api_ns", "catalogue_load_ns", "crypto_materialization_ns", "materialization_ns", "query_embedding_ns", "ranking_ns", "end_to_end_ns")) <= timings.keys()
        assert timings["api_ns"] >= timings["end_to_end_ns"] > 0
    asyncio.run(run())


def test_hybrid_small_candidate_limits_match_actual_legacy(retrieval_pair, monkeypatch):
    from app.core.config import settings
    c = retrieval_pair
    provider = c.app.state.secure_runtime.embedding_provider
    for row, done in c.rows:
        index_processing_result(record_id=row["upload_id"], processing_job_id=done["job_id"], user_id=c.session["user"]["user_id"], provider=provider, session_factory=c.legacy_sessions)
        assert index_secure(c, row, done).status_code == 200
    for limit in (1, 2, 20):
        monkeypatch.setattr(settings, "retrieval_candidate_limit", limit)
        expected = search_room(room_id=c.room["room_id"], user_id=c.session["user"]["user_id"], query="아버지 제주도",
            arm="hybrid_rrf", k=2, provider=provider, session_factory=c.legacy_sessions, candidate_limit=limit)
        actual = search_secure(c, "hybrid_rrf", "아버지 제주도", 2)
        assert actual.status_code == 200
        assert [h["chunk_id"] for h in actual.json()["hits"]] == [h.chunk.id for h in expected]


def test_cross_room_records_and_principals_never_become_evidence(media_client, monkeypatch, record_answer_provider):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    other, _, _ = c.signup("foreign", "foreign@example.com")
    other_room, _ = create_room(c, other)
    owner_auth, own_room = c.auth, c.room
    c.auth = {"Authorization": "Bearer " + other["access_token"]}
    for response in (search_secure(c), ask_secure(c), index_secure(c, row, done)):
        assert response.status_code in (404, 503) and QUOTE not in response.text
    c.room = other_room
    assert search_secure(c).json()["hits"] == []
    assert ask_secure(c).json()["gate_route"] == "NO_RECORD"
    assert not record_answer_provider
    c.auth, c.room = owner_auth, own_room
    # The authenticated entity source helper must not acquire another user's KEK.
    original_load = c.app.state.secure_runtime.vault.load
    def deny_foreign(ref):
        assert not (ref.domain == "user" and str(ref.owner_id) == other["user"]["user_id"])
        return original_load(ref)
    monkeypatch.setattr(c.app.state.secure_runtime.vault, "load", deny_foreign)
    response = ask_secure(c)
    assert response.status_code == 200 and response.json()["answer"] == RECORD_ANSWER
    assert len(record_answer_provider) == 1
    assert {citation["record_id"] for citation in response.json()["citations"]} == {row["upload_id"]}
    assert QUOTE in "\n".join(record_answer_provider[0].prompt_package["retrieved_evidence"])
