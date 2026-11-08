import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from test_processing_api import _setup, _teardown, USER_ID, OTHER_USER_ID, ROOM_ID, RECORD_ID, IMAGE_FILE_ID
from app.api import deps
from app.main import app
from app.models.family import FamilyMember
from app.models.processing_job import ProcessingJob
from app.models.upload import UploadFile


@pytest.fixture
def review_case():
    engine, tracker, old_sl, old_ocr = _setup()
    raw = json.dumps(dict(text="Original evidence", extractor="AUTO_OCR", extractor_id="synthetic", regions=[]))
    with tracker.session_maker() as db:
        file = db.get(UploadFile, IMAGE_FILE_ID)
        file.file_uploader_user_id = OTHER_USER_ID
        file.created_at = datetime.now(timezone.utc) + timedelta(days=1)
        db.add(FamilyMember(id="review-member", room_id=ROOM_ID, user_id=OTHER_USER_ID, role="member"))
        db.add(ProcessingJob(id="review-job", record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=IMAGE_FILE_ID,
                             processor="ocr", status="SUCCEEDED", attempt=2, result_json=raw))
        db.commit()
    app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=OTHER_USER_ID)
    try:
        yield TestClient(app), tracker, raw
    finally:
        _teardown(engine, old_sl, old_ocr)


@pytest.mark.parametrize("action,status,confirmed", [("approve","confirmed",True),("reject","rejected",False)])
def test_review_persistence_idempotence_immutable_result(review_case, action, status, confirmed):
    client, tracker, raw = review_case
    path = "/api/v1/processing-jobs/review-job/review"
    first = client.post(path, json={"action":action})
    assert first.status_code == 200, first.text
    assert first.json()["review_status"] == status
    assert first.json()["user_confirmed"] is confirmed
    assert client.post(path,json={"action":action}).json() == first.json()
    assert client.get(path).json() == first.json()
    assert client.post(path,json={"action":"reject" if action == "approve" else "approve"}).status_code == 409
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob,"review-job")
        assert job.result_json == raw
        assert json.loads(job.review_json)["source_attempt"] == 2
    assert client.get("/api/v1/processing-jobs/review-job").json()["result"] == (json.loads(raw) if action == "approve" else None)


@pytest.mark.parametrize("change,code", [("owner",404),("foreign",404),("missing",404),("null",409),
                                          ("stt",409),("failed",409),("invalid",409),("replacement",409),
                                          ("removed",404),("unpersisted",401)])
def test_authorization_and_source_fail_closed(review_case, change, code):
    client, tracker, _ = review_case
    job_id = "review-job"
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob,job_id)
        if change == "owner":
            app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=USER_ID)
        elif change == "foreign":
            db.execute(delete(FamilyMember).where(FamilyMember.id == "review-member"))
        elif change == "missing":
            job_id = "missing"
        elif change == "null":
            db.get(UploadFile,IMAGE_FILE_ID).file_uploader_user_id = None
        elif change == "stt":
            job.processor = "stt"
        elif change == "failed":
            job.status = "FAILED"
        elif change == "invalid":
            job.result_json = "{}"
        elif change == "replacement":
            db.add(UploadFile(id="replacement",upload_id=RECORD_ID,created_at=datetime.now(timezone.utc)+timedelta(days=2)))
        elif change == "removed":
            db.execute(delete(FamilyMember).where(FamilyMember.id == "review-member"))
        elif change == "unpersisted":
            app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id="ghost")
        db.commit()
    response = client.post(f"/api/v1/processing-jobs/{job_id}/review",json={"action":"approve"})
    assert response.status_code == code, response.text


@pytest.mark.parametrize("body", [{"action":"edit"},{"action":"approve","manual_text":"x"},
                                  {"action":"approve","corrected_text":"x"},
                                  {"action":"reject","replacement_text":"x"},{}])
def test_strict_body(review_case, body):
    assert review_case[0].post("/api/v1/processing-jobs/review-job/review",json=body).status_code == 422


@pytest.mark.parametrize("change", ["attempt","result","replacement","membership"])
def test_conditional_write_revalidates_committed_race(review_case, monkeypatch, change):
    from sqlalchemy.sql.dml import Update
    from app.services.processing_jobs import review_ocr_result, IllegalTransition, ProcessingJobNotFound
    _, tracker, _ = review_case
    with tracker.session_maker() as db:
        execute = db.execute
        fired = False
        def racing(statement, *args, **kwargs):
            nonlocal fired
            if isinstance(statement, Update) and not fired:
                fired = True
                with tracker.session_maker() as other:
                    if change == "attempt":
                        other.execute(update(ProcessingJob).where(ProcessingJob.id=="review-job").values(attempt=3))
                    elif change == "result":
                        other.execute(update(ProcessingJob).where(ProcessingJob.id=="review-job").values(result_json=json.dumps(dict(text="changed",extractor="AUTO_OCR",extractor_id="synthetic"))))
                    elif change == "replacement":
                        other.add(UploadFile(id="racing-file",upload_id=RECORD_ID,created_at=datetime.now(timezone.utc)+timedelta(days=2)))
                    else:
                        other.execute(delete(FamilyMember).where(FamilyMember.id=="review-member"))
                    other.commit()
            return execute(statement,*args,**kwargs)
        monkeypatch.setattr(db,"execute",racing)
        with pytest.raises((IllegalTransition,ProcessingJobNotFound)):
            review_ocr_result(db,"review-job",OTHER_USER_ID,"approve")
    with tracker.session_maker() as db:
        assert db.get(ProcessingJob,"review-job").review_json is None


@pytest.mark.parametrize("action", ["approve"])
def test_approved_evidence_search_and_source_are_unchanged(action):
    import test_retrieval_indexing as ri
    from app.services.processing_jobs import review_ocr_result
    engine, tracker = ri.setup_indexing()
    try:
        with tracker.session_maker() as db:
            db.get(UploadFile,ri.FILE_ID).file_uploader_user_id = ri.USER_ID
            db.commit()
        ri.index(tracker,ri.FakeEmbeddingProvider(tracker))
        before = ri.persisted_rows(tracker)
        search = ri.searchable_ids(tracker)
        assert all(search.values())
        with tracker.session_maker() as db:
            raw = db.get(ProcessingJob,ri.JOB_ID).result_json
            response = review_ocr_result(db,ri.JOB_ID,ri.USER_ID,action)
            assert response["user_confirmed"] == (action == "approve")
        from app.ai.grounded_answering import _source_is_current, build_fact_sheet, RetrievalChunk
        from app.models.retrieval import RecordChunk
        from sqlalchemy import select
        with tracker.session_maker() as db:
            rows = db.scalars(select(RecordChunk)).all()
            facts = build_fact_sheet([RetrievalChunk(**{key:getattr(row,key)
                for key in RetrievalChunk.__dataclass_fields__}) for row in rows]).facts
            assert facts and all(_source_is_current(db,fact,ri.ROOM_ID) for fact in facts)
        assert ri.persisted_rows(tracker) == before
        assert ri.searchable_ids(tracker) == search
        with tracker.session_maker() as db:
            assert db.get(ProcessingJob,ri.JOB_ID).result_json == raw
        assert ri.index(tracker,ri.FakeEmbeddingProvider(tracker)).source_result_sha256 == before[0][0][2]
    finally:
        engine.dispose()


@pytest.mark.parametrize("metadata", ["{}", "[]", "invalid", '{"status":"unknown"}',
                                       '{"status":"confirmed","source_attempt":999}'])
def test_existing_malformed_review_fails_closed(review_case, metadata):
    client, tracker, _ = review_case
    with tracker.session_maker() as db:
        db.get(ProcessingJob,"review-job").review_json = metadata
        db.commit()
    path = "/api/v1/processing-jobs/review-job/review"
    assert client.post(path,json={"action":"approve"}).status_code == 409
    assert client.get(path).status_code == 409


def test_exact_secure0005_processing_mapping_omits_review_column(tmp_path):
    from alembic import command
    from sqlalchemy import inspect, select
    from sqlalchemy.orm import sessionmaker
    from scripts.adopt_alembic import _build_alembic_config, verify_revision
    from conftest import fk_enforced_engine
    from test_migrations import _seed_retrieval_parents
    from app.services.processing_jobs import create_job, serialize_processing_job
    url = f"sqlite:///{tmp_path / 'secure.sqlite'}"
    command.upgrade(_build_alembic_config(url),"0005_secure_catalogue")
    engine = fk_enforced_engine(url)
    try:
        _seed_retrieval_parents(engine)
        with engine.connect() as db:
            assert verify_revision(db,"0005_secure_catalogue") == []
            assert "review_json" not in {c["name"] for c in inspect(db).get_columns("processing_jobs")}
        with sessionmaker(bind=engine)() as db:
            job = db.scalar(select(ProcessingJob))
            assert serialize_processing_job(job)["job_id"] == job.id
            db.add(ProcessingJob(id="secure-new",record_id=job.record_id,room_id=job.room_id,
                                 upload_file_id=job.upload_file_id,processor="stt"))
            db.commit()
            assert serialize_processing_job(db.get(ProcessingJob,"secure-new"))["status"] == "PENDING"
    finally:
        engine.dispose()


def test_unauthenticated_review_returns401(review_case):
    client, _, _ = review_case
    app.dependency_overrides.pop(deps.get_authenticated_principal)
    path = "/api/v1/processing-jobs/review-job/review"
    assert client.post(path,json={"action":"approve"}).status_code == 401
    assert client.get(path).status_code == 401


def test_same_room_member_who_is_not_uploader_is_opaque404(review_case):
    from app.models.user import User
    from app.models.auth_identity import AuthIdentity
    client, tracker, _ = review_case
    with tracker.session_maker() as db:
        db.add(User(id="sibling",google_sub=None,username="sibling",email="sibling@test.invalid",name="Sibling"))
        db.flush()
        db.add(AuthIdentity(id="sibling-ai",user_id="sibling",provider="local",provider_subject="sibling",credential_hash="x"))
        db.add(FamilyMember(id="sibling-member",room_id=ROOM_ID,user_id="sibling",role="member"))
        db.commit()
    app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id="sibling")
    assert client.post("/api/v1/processing-jobs/review-job/review",json={"action":"approve"}).status_code == 404


def test_missing_file_linkage_fails_closed(review_case,monkeypatch):
    from app.services.processing_jobs import review_ocr_result, ProcessingJobNotFound
    _, tracker, _ = review_case
    with tracker.session_maker() as db:
        get = db.get
        monkeypatch.setattr(db,"get",lambda model,id,*a,**kw: None if model is UploadFile else get(model,id,*a,**kw))
        with pytest.raises(ProcessingJobNotFound):
            review_ocr_result(db,"review-job",OTHER_USER_ID,"approve")


def test_stt_review_is_supported_with_matching_extractor(review_case):
    client, tracker, _ = review_case
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob, "review-job")
        job.processor = "stt"
        value = json.loads(job.result_json)
        value["extractor"] = "AUTO_STT"
        job.result_json = json.dumps(value)
        db.commit()
    response = client.post("/api/v1/processing-jobs/review-job/review", json={"action": "approve"})
    assert response.status_code == 200, response.text
    assert response.json()["user_confirmed"] is True
