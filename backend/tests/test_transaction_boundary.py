"""Legacy script snapshot boundaries; public HTTP safety is tested in test_chat_demo_canonical."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine
from app.ai import demo_service
from app.ai.providers.base import InferenceResponse
from app.api import deps
from app.core import database
from app.core.database import Base
from app.main import app
from app.models.auth_identity import AuthIdentity, PrincipalNotPersisted
from app.schemas.ai import AIDemoChatRequest
from app.models.family import FamilyMember, FamilyRoom
from app.models.upload import Upload
from app.models.user import User

ROOM_ID = "room-tb-1"
OTHER_ROOM_ID = "room-tb-2"
USER_ID = "user-tb-1"
OTHER_USER_ID = "user-tb-2"


class SessionOpenTracker:
    """Wraps `get_db` so `session.close()` is observed exactly once, regardless
    of how many times it's called (our own early close + FastAPI's generator
    teardown both call it)."""

    def __init__(self, engine):
        self.session_maker = sessionmaker(bind=engine)
        self.open_count = 0

    def __call__(self):
        db = self.session_maker()
        self.open_count += 1
        already_closed = False
        original_close = db.close

        def tracked_close():
            nonlocal already_closed
            if not already_closed:
                already_closed = True
                self.open_count -= 1
            original_close()

        db.close = tracked_close
        return db

    def get_db(self):
        db = self()
        try:
            yield db
        finally:
            db.close()


class RecordingProvider:
    def __init__(self, tracker: SessionOpenTracker, output_text: str = "기록에는 2018년 여름 광안리 해변 산책이 남아 있습니다."):
        self.tracker = tracker
        self.output_text = output_text
        self.calls = 0
        self.open_count_at_call: list[int] = []

    def generate(self, request):
        self.calls += 1
        self.open_count_at_call.append(self.tracker.open_count)
        return InferenceResponse(provider="test", mode="remote", output_text=self.output_text, model_id=request.model_id)


def _seed(session_maker):
    from app.services.record_policy import register_legacy_record
    with session_maker() as db:
        db.add(User(id=USER_ID, google_sub=None, username="tb_user", password_hash="x", email="tb@example.test", name="TB User"))
        db.add(User(id=OTHER_USER_ID, google_sub=None, username="tb_other", password_hash="x", email="tb-other@example.test", name="TB Other"))
        db.flush()  # parents before children: FK-enforced engines require users to exist first
        db.add(AuthIdentity(id="ai-tb-1", user_id=USER_ID, provider="local", provider_subject="tb_user", credential_hash="x"))
        db.add(AuthIdentity(id="ai-tb-2", user_id=OTHER_USER_ID, provider="local", provider_subject="tb_other", credential_hash="x"))
        db.add(FamilyRoom(id=ROOM_ID, name="TB 가족방", invite_code="TB0001", owner_user_id=USER_ID))
        db.add(FamilyRoom(id=OTHER_ROOM_ID, name="TB 남의 방", invite_code="TB0002", owner_user_id=OTHER_USER_ID))
        db.flush()
        db.add(FamilyMember(id="fm-tb-1", room_id=ROOM_ID, user_id=USER_ID, role="owner"))
        db.add(FamilyMember(id="fm-tb-2", room_id=OTHER_ROOM_ID, user_id=OTHER_USER_ID, role="owner"))
        db.add(Upload(
            id="upload-tb-1", room_id=ROOM_ID, uploader_user_id=USER_ID, type="text",
            title="부산 가족 여행 사진 메모",
            description="2018년 여름 광안리 해변 산책, 저녁 무렵 바람이 시원했다고 남김",
            status="uploaded",
        ))
        db.add(Upload(
            id="upload-tb-secret", room_id=OTHER_ROOM_ID, uploader_user_id=OTHER_USER_ID, type="text",
            title="남의 비밀 기록", description="이웃집 비밀번호", status="uploaded",
        ))
        db.flush()
        register_legacy_record(db, "upload-tb-1")
        register_legacy_record(db, "upload-tb-secret")
        db.commit()


def _setup():
    engine = fk_enforced_engine("sqlite://")
    Base.metadata.create_all(engine)
    tracker = SessionOpenTracker(engine)
    _seed(tracker.session_maker)

    app.dependency_overrides[database.get_db] = tracker.get_db
    app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=USER_ID)

    provider = RecordingProvider(tracker)
    old_provider_factory = demo_service.get_provider_for_model
    demo_service.get_provider_for_model = lambda model_id: provider

    return engine, tracker, provider, old_provider_factory


def _teardown(engine, old_provider_factory):
    demo_service.get_provider_for_model = old_provider_factory
    app.dependency_overrides.clear()
    engine.dispose()


def _snapshot(tracker, query, **extra):
    return demo_service.load_demo_chat_snapshot(
        tracker, deps.AuthenticatedPrincipal(user_id=USER_ID),
        AIDemoChatRequest(room_id=ROOM_ID, model_id="gemma-4-e2b-device",
                          persona_id="father-calm", query=query, **extra),
    )


def test_answer_snapshot_closes_session_before_provider_generate():
    engine, tracker, provider, old_provider_factory = _setup()
    try:
        result = demo_service.generate_demo_chat_response(
            _snapshot(tracker, "부산 여행이 몇 년도였는지 알려줘."))
        assert result["gate_route"] == "ANSWER"
        assert provider.calls == 1
        assert provider.open_count_at_call == [0]
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_provider_factory)


def test_rule_only_snapshot_never_calls_provider_and_closes_session():
    engine, tracker, provider, old_provider_factory = _setup()
    try:
        result = demo_service.generate_demo_chat_response(
            _snapshot(tracker, "부산 여행의 정확한 날짜(몇 월 며칠)를 알려줘."))
        assert result["gate_route"] == "REFUSE"
        assert result["gate_action"] == "llm_not_called"
        assert provider.calls == 0
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_provider_factory)


def test_cross_room_selected_record_rejected_by_legacy_snapshot():
    from app.services.record_policy import RecordAccessDenied
    engine, tracker, provider, old_provider_factory = _setup()
    try:
        with pytest.raises(RecordAccessDenied) as denial:
            _snapshot(tracker, "생일이 언제야?", selected_record_id="upload-tb-secret")
        assert denial.value.status_code == 404
        assert denial.value.detail == {"code": "RESOURCE_NOT_AVAILABLE"}
        assert provider.calls == 0
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_provider_factory)


def test_legacy_snapshot_rejects_principal_without_persisted_identity():
    engine, tracker, provider, old_factory = _setup()
    try:
        with tracker.session_maker() as db:
            db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
            db.commit()
        with pytest.raises(PrincipalNotPersisted):
            _snapshot(tracker, "광안리 산책 기억나?")
        assert provider.calls == 0
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_factory)


def test_compat_wrapper_closes_session_before_generate():
    engine, tracker, provider, old_provider_factory = _setup()
    try:
        result = demo_service.build_demo_chat_response(
            session_factory=tracker,
            room_id=ROOM_ID,
            user_id=USER_ID,
            model_id="gemma-4-e2b-device",
            persona_id="father-calm",
            query="부산 여행이 몇 년도였는지 알려줘.",
        )
        assert result["gate_route"] == "ANSWER"
        assert provider.calls == 1
        assert provider.open_count_at_call == [0]
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_provider_factory)
