"""E2E 실기동 검증 — 실제 FastAPI 앱을 TestClient로 띄워 /api/v1/ai/chat-demo 전 경로 검증.

    python3 backend/scripts/e2e_gate_demo.py

- DB: in-memory SQLite (실 모델로 create_all + 데모 가족 시드 — 클라우드 불필요)
- 인증: dependency_overrides로 데모 유저 주입
- LLM: 스텁 provider (시나리오별 환각/근거/빈응답 출력, 호출 횟수 계수)
검증 항목: rule_gate 경로가 LLM을 안 부르는지, 출력 게이트가 환각을 막는지,
gate_* 필드가 HTTP 응답 JSON까지 살아서 나오는지(스키마 탈락 회귀 방지), 빈응답 재시도.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

# app 모듈 import 전에 주입 — 로컬 E2E는 sqlite로 (config가 env DATABASE_URL을 읽음)
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_CLOUD_SQL_CONNECTOR", "false")
os.environ.setdefault("USE_GCS_MEDIA_STORAGE", "false")

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 로컬엔 Cloud SQL 커넥터가 없어도 E2E는 sqlite로 돈다 — import 시점 의존성만 스텁.
# (get_db는 아래에서 오버라이드하므로 커넥터는 실제로 호출되지 않는다)
import types  # noqa: E402

if "google.cloud.sql.connector" not in sys.modules:
    _stub = types.ModuleType("google.cloud.sql.connector")

    class _Connector:  # pragma: no cover - 호출되지 않음
        def __init__(self, *a, **k):
            pass

        def connect(self, *a, **k):
            raise RuntimeError("E2E stub — 실 DB 연결은 비활성")

    _stub.Connector = _Connector
    _stub.IPTypes = types.SimpleNamespace(PUBLIC="PUBLIC", PRIVATE="PRIVATE")
    google_mod = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_mod = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    sql_mod = sys.modules.setdefault("google.cloud.sql", types.ModuleType("google.cloud.sql"))
    google_mod.cloud = cloud_mod
    cloud_mod.sql = sql_mod
    sql_mod.connector = _stub
    sys.modules["google.cloud.sql.connector"] = _stub

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api import deps  # noqa: E402
from app.core import database  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.main import app  # noqa: E402
from app.models.auth_identity import AuthIdentity  # noqa: E402
from app.models.family import FamilyMember, FamilyRoom  # noqa: E402
from app.models.upload import Upload  # noqa: E402
from app.models.user import User  # noqa: E402
from app.ai.providers.base import InferenceResponse  # noqa: E402
from app.ai import demo_service  # noqa: E402

# ---------- in-memory DB + 시드 ----------
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(engine)
TestSession = sessionmaker(bind=engine)

ROOM_ID = "room-e2e"
USER_ID = "user-e2e"

with TestSession() as db:
    user = User(id=USER_ID, google_sub="e2e-google-sub", username="e2e",
                email="e2e@example.com", name="김하늘")
    db.add(user)
    db.flush()  # parents before children: FK-enforced engine requires the user to exist first
    db.add(AuthIdentity(id="identity-e2e", user_id=USER_ID, provider="google",
                        provider_subject="e2e-google-sub", provider_email="e2e@example.com"))
    db.add(FamilyRoom(id=ROOM_ID, name="E2E 가족방", invite_code="E2E123",
                      owner_user_id=USER_ID))
    db.flush()
    db.add(FamilyMember(id="fm-1", room_id=ROOM_ID, user_id=USER_ID, role="owner",
                        relation_to_related_user="아버지"))
    seeds = [
        ("text", "병원 예약 메모", "아버지 검진은 오전 10시, 접수는 20분 전까지 도착"),
        ("image", "예약 확인 문자 OCR", "검진 예약 11:00, 10분 전 내원 요청"),
        ("image", "부산 가족 여행 사진", "2018년 여름 광안리 해변 산책, 바람이 시원했다고 남김"),
        ("text", "막내 생일 메모", "막내 생일은 5월 3일, 케이크는 초코로 준비"),
    ]
    for i, (utype, title, desc) in enumerate(seeds):
        db.add(Upload(id=f"up-{i}", room_id=ROOM_ID, uploader_user_id=USER_ID,
                      type=utype, title=title, description=desc))
    db.commit()


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


def override_current_user():
    with TestSession() as db:
        return db.get(User, USER_ID)


app.dependency_overrides[database.get_db] = override_get_db
app.dependency_overrides[deps.get_current_user] = override_current_user
app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=USER_ID)

# ---------- 스텁 provider ----------
class StubProvider:
    def __init__(self):
        self.calls = 0
        self.script: list[str] = []
        self.fail_next = False  # provider 장애 시뮬레이션 (mode="error")

    def generate(self, request):
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            return InferenceResponse(provider="stub", mode="error", output_text="",
                                     model_id=request.model_id)
        text = self.script.pop(0) if self.script else "기록에 있는 내용만 정리해 드렸습니다."
        return InferenceResponse(provider="stub", mode="remote", output_text=text,
                                 model_id=request.model_id)


stub = StubProvider()
demo_service.get_provider_for_model = lambda model_id: stub  # 서비스 계층 주입

client = TestClient(app)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def chat(query: str) -> dict:
    r = client.post("/api/v1/ai/chat-demo", json={
        "room_id": ROOM_ID, "model_id": "gemma-4-e2b-device",
        "persona_id": "father-calm", "query": query})
    assert r.status_code == 200, r.text
    return r.json()


print("① rule_gate 경로 (LLM 미호출)")
before = stub.calls
res = chat("예약한 병원 이름이 뭐였는지 알려줘.")
check("병원 이름 → REFUSE + rule_gate", res["answer_source"] == "rule_gate"
      and res["gate_route"] == "REFUSE", f"{res['answer_source']}/{res.get('gate_route')}")
check("REFUSE 경로 LLM 호출 0회", stub.calls == before)
check("gate_route가 HTTP JSON에 실림 (스키마 회귀 방지)", "gate_route" in res)

res = chat("아버지 검진이 몇 시였는지 알려줘.")
check("시각 모순 → CONFLICT 양립", res.get("gate_route") == "CONFLICT"
      and "10시" in res["answer"] and "11시" in res["answer"])

print("② 출력 게이트 (환각 차단)")
stub.script = ["부산 여행은 2018년 7월 15일이었고 해운대식당에서 저녁을 드셨습니다."]
res = chat("부산 여행 기록 요약해줘.")
check("환각(7/15·해운대식당) 문장 차단", "15" not in res["answer"].split("「")[0]
      and "해운대식당" not in res["answer"], res.get("gate_action", ""))

stub.script = ["기록에는 2018년 여름 광안리 해변 산책이 남아 있습니다."]
res = chat("부산 여행 기록 요약해줘.")
check("근거 있는 응답 → 통과", res["gate_action"] == "pass" and "2018" in res["answer"])

print("③ 빈응답 재시도 (B1 완화)")
stub.script = ["", "막내 생일은 5월 3일로 기록되어 있습니다."]
before = stub.calls
res = chat("막내 생일 기록 요약해줘.")
check("빈응답 → 1회 재시도 후 정상 응답", stub.calls == before + 2
      and "5월 3일" in res["answer"], f"calls+{stub.calls - before}")

print("⑤ 오타/맞춤법 내성")
stub.script = ["막내 생일은 5월 3일로 기록되어 있습니다."]
res = chat("막내 생일이 몇일이야?")  # 몇일(오기) → 며칠 접기 → date_md 판정
check("'몇일' 오기 → 날짜 질문으로 정상 라우팅·응답", res.get("gate_route") == "ANSWER"
      and "5월 3일" in res["answer"], res.get("gate_route", ""))

before = stub.calls
res = chat("검진 예약이 몇일이었지?")  # 병원 기록엔 월/일 없음 → 규칙 거절
check("'몇일' 오기 + 기록에 월일 없음 → REFUSE (LLM 0회)",
      res.get("gate_route") == "REFUSE" and stub.calls == before)

print("⑥ CLARIFY는 LLM 미호출 — 재작성 경로 제거 (감사 9-2 계약)")
# 체크포인트 9: CLARIFY의 LLM 재작성 왕복을 제거 — 오타·표기는 결정론 정규화가 처리하고,
# 모호 질문은 결정론 후보 제시로 해소한다. provider 호출 0회가 계약이다.
stub.script = []
before = stub.calls
res = chat("그때 그거 언제였지?")  # 접점 0 → CLARIFY → 후보 제시 (LLM 0회)
check("모호 질문 → LLM 0회 + 후보형 CLARIFY",
      res.get("gate_route") == "CLARIFY" and stub.calls == before
      and "생일" in res["answer"],
      f"calls+{stub.calls - before}, detail={res.get('gate_detail')}")

# 재작성 재도입 시의 채택 계약(rewrite_preserves_intent)은 회귀로 유지한다
from app.ai.gates.answer_router import rewrite_preserves_intent  # noqa: E402
from app.ai.gates.entity_index import build_room_entity_index  # noqa: E402
_idx = build_room_entity_index(["막내"], [])
check("표기 복원 재작성(몇일→며칠)은 채택 가능",
      rewrite_preserves_intent("막내 생일이 몇일이야?", "막내 생일이 며칠이야?", _idx))
check("인물 추가 재작성은 폐기",
      not rewrite_preserves_intent("그때 그거 언제였지?", "막내 생일이 언제였지?", _idx))
check("주제 하이재킹 재작성(부산 여행)은 폐기",
      not rewrite_preserves_intent("그때 그건 언제였어?", "부산 여행은 언제였어?", _idx))
check("주제 하이재킹 재작성(장례식)은 폐기",
      not rewrite_preserves_intent("그때 그건 언제였어?", "장례식은 언제였어?", _idx))
check("의문사 부류 변경(왜→어떻게)은 폐기",
      not rewrite_preserves_intent("왜 그랬어?", "어떻게 했어?", _idx))

print("⑦ Phase 0 — 진단 비노출·질의값 비신뢰·프로바이더 실패 명시화")
stub.script = ["기록에는 2018년 여름 광안리 해변 산책이 남아 있습니다."]
res = chat("부산 여행 기록 요약해줘.")
check("진단 필드(provider_output_preview/persona_preview/prompt_package) 비노출",
      all(k not in res for k in ("provider_output_preview", "persona_preview", "prompt_package")),
      str([k for k in ("provider_output_preview", "persona_preview", "prompt_package") if k in res]))
_ev = " ".join(res.get("retrieved_evidence", []))
check("공개 evidence에 내부 메타(source=/saved_at=/tags=/confidence=) 비노출",
      all(m not in _ev for m in ("source=", "saved_at=", "tags=", "confidence=")),
      _ev[:80])
check("공개 evidence는 가족 가독형 기록 원문", bool(res.get("retrieved_evidence"))
      and "광안리" in _ev)

stub.script = ["부산 여행은 2018년 7월 15일이었습니다."]
res = chat("부산 여행 7월 15일에 갔던 거 맞지?")  # 질문이 심은 날짜 — 근거 아님
check("질의가 심은 '7월 15일' 복창 → 최종 답변에서 차단",
      "7월 15일" not in res["answer"], res.get("gate_action", ""))

stub.fail_next = True
res = chat("부산 여행이 몇 년도였는지 알려줘.")  # ANSWER 라우트인데 provider 장애
check("프로바이더 장애 → 명시적 안전 상태 (검증된 척 금지)",
      res.get("gate_action") == "provider_unavailable"
      and res["answer_source"] == "fallback"
      and "받지 못해" in res["answer"] and "개인화" not in res["answer"],
      f"{res.get('gate_action')}/{res['answer_source']}")

print("⑧ 실행형 CLARIFY — 재작성 실패 시 후보 기록 제시 (UX-G02)")
stub.script = ["질문을 특정하지 못했습니다."]  # 재작성이 쓸모없는 문장을 내놓는 상황
res = chat("그때 그건 언제였어?")
check("모호 질문 + 재작성 실패 → 후보 기록 제시 CLARIFY",
      res.get("gate_route") == "CLARIFY"
      and "부산" in res["answer"] and "생일" in res["answer"],
      res["answer"][:70])
check("clarify_candidates 필드가 HTTP JSON에 실림",
      isinstance(res.get("clarify_candidates"), list) and len(res["clarify_candidates"]) >= 2,
      str(res.get("clarify_candidates")))
check("clarify_options(record_id/label) 안정 식별자 계약이 HTTP JSON에 실림",
      isinstance(res.get("clarify_options"), list) and len(res["clarify_options"]) >= 2
      and all(o.get("record_id") and o.get("label") for o in res["clarify_options"]),
      str(res.get("clarify_options"))[:80])

print("⑨ 후보 선택(selected_record_id) — 같은 제목 구분·room 격리 (감사 9-4)")
with TestSession() as db:
    db.add(Upload(id="up-b1", room_id=ROOM_ID, uploader_user_id=USER_ID, type="text",
                  title="생일 메모", description="막내 생일은 5월 3일"))
    db.add(Upload(id="up-b2", room_id=ROOM_ID, uploader_user_id=USER_ID, type="text",
                  title="생일 메모", description="아버지 생일은 3월 1일"))
    other_user = User(id="user-other", google_sub="other-sub", username="other",
                      email="other@example.com", name="박이웃")
    db.add(other_user)
    db.flush()  # parents before children: FK-enforced engine requires the user to exist first
    db.add(FamilyRoom(id="room-other", name="남의 방", invite_code="OTHER1",
                      owner_user_id="user-other"))
    db.flush()
    db.add(FamilyMember(id="fm-other", room_id="room-other", user_id="user-other",
                        role="owner"))
    db.add(Upload(id="up-secret", room_id="room-other", uploader_user_id="user-other",
                  type="text", title="비밀 메모", description="이웃집 비밀번호 1234"))
    db.commit()


def chat_selected(record_id: str, room_id: str = ROOM_ID):
    return client.post("/api/v1/ai/chat-demo", json={
        "room_id": room_id, "model_id": "gemma-4-e2b-device",
        "persona_id": "father-calm", "query": "생일이 언제야?",
        "selected_record_id": record_id})


# 실제 앱 흐름 계약 (감사 10-4): ① 모호 질문 → CLARIFY의 clarify_options에서
# 같은 제목 2건이 '구분되는 label + 서로 다른 record_id'로 내려오고, ② 사용자가 고른
# 옵션의 record_id(응답에서 획득 — 직접 주입 아님)로 후속 질문하면 그 기록으로 고정된다.
res_c = chat("그때 그 기념일 언제였지?")
options = res_c.get("clarify_options") or []
labels = [o["label"] for o in options]
same_title = [o for o in options if o["label"].startswith("생일 메모")]
check("같은 제목 2건이 모두 후보에 포함 + label 구분(인물 병기)",
      res_c.get("gate_route") == "CLARIFY" and len(same_title) >= 2
      and len(set(o["label"] for o in same_title)) == len(same_title)
      and len(set(o["record_id"] for o in same_title)) == len(same_title),
      str(labels))

picked = next((o for o in same_title if "막내" in o["label"]), None)
check("사용자 구분 정보(막내)로 옵션 식별 가능", picked is not None, str(labels))
stub.script = ["막내 생일은 5월 3일로 기록되어 있습니다."]
r1 = chat_selected(picked["record_id"]) if picked else None
res1 = r1.json() if picked else {}
picked_f = next((o for o in same_title if "아버지" in o["label"]), None)
stub.script = ["아버지 생일은 3월 1일로 기록되어 있습니다."]
r2 = chat_selected(picked_f["record_id"]) if picked_f else None
res2 = r2.json() if picked_f else {}
check("후보에서 얻은 record_id로 각각 선택 → 근거가 서로 다르게 고정",
      picked and picked_f and r1.status_code == 200 and r2.status_code == 200
      and "5월 3일" in " ".join(res1["retrieved_evidence"])
      and "3월 1일" in " ".join(res2["retrieved_evidence"])
      and res1["retrieved_evidence"] != res2["retrieved_evidence"])
check("선택 시 모호 주어 CLARIFY 없이 해당 기록으로 진행",
      res1.get("gate_route") == "ANSWER" and "5월 3일" in res1["answer"],
      f"{res1.get('gate_route')}")

r_cross = chat_selected("up-secret")  # 내 방 room_id + 남의 방 record_id
check("타 방 기록 id 선택 → 404 (존재 여부도 비노출)", r_cross.status_code == 404,
      str(r_cross.status_code))
r_foreign = chat_selected("up-secret", room_id="room-other")  # 남의 방 자체를 지정
check("내가 속하지 않은 방 지정 → 200 아님 (멤버십 검증)", r_foreign.status_code != 200,
      str(r_foreign.status_code))

print("④ 기록 없는 방 → NO_RECORD")
with TestSession() as db:
    db.add(FamilyRoom(id="room-empty", name="빈 방", invite_code="EMPTY1",
                      owner_user_id=USER_ID))
    db.flush()  # parents before children: FK-enforced engine requires the room to exist first
    db.add(FamilyMember(id="fm-2", room_id="room-empty", user_id=USER_ID, role="owner"))
    db.commit()
r = client.post("/api/v1/ai/chat-demo", json={
    "room_id": "room-empty", "model_id": "gemma-4-e2b-device",
    "persona_id": "father-calm", "query": "우리 가족 여행 기록 알려줘."})
res = r.json()
check("기록 0건 → NO_RECORD rule_gate", res.get("gate_route") == "NO_RECORD"
      and res["answer_source"] == "rule_gate")

print()
if failures:
    print(f"❌ E2E 실패 {len(failures)}건: {failures}")
    raise SystemExit(1)
print("✅ E2E 전 경로 통과 — 게이트가 서버 API까지 살아서 도달")
