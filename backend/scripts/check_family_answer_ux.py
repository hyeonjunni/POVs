"""가족 답변 UX 회귀/알려진 간극 스위트.

기존 check_gates.py의 안전 회귀와 분리한다.

    backend/.venv/bin/python backend/scripts/check_family_answer_ux.py
    backend/.venv/bin/python backend/scripts/check_family_answer_ux.py --strict

기본 모드는 baseline 실패만 종료 코드 1로 처리한다. known_gap은 개선 목표를 만족하지
못하면 현재 위험으로 보고하되 빌드를 깨지 않는다. --strict는 known_gap도 실패로 처리한다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.ai import demo_service  # noqa: E402
from app.ai.gates.answer_router import route_query  # noqa: E402
from app.ai.gates.entity_index import build_room_entity_index  # noqa: E402
from app.ai.gates.output_gate import apply_output_gate  # noqa: E402
from app.ai.providers.base import InferenceResponse  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.models.auth_identity import AuthIdentity  # noqa: E402
from app.models.family import FamilyMember, FamilyRoom  # noqa: E402
from app.models.upload import Upload  # noqa: E402
from app.models.user import User  # noqa: E402


CASES_PATH = BACKEND / "tests" / "family_answer_ux_cases.json"


class FixedProvider:
    def __init__(self, output_text: str):
        self.output_text = output_text
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return InferenceResponse(
            provider="ux-test",
            mode="remote",
            output_text=self.output_text,
            model_id=request.model_id,
        )


def _matches_expectation(actual: dict, expect: dict) -> tuple[bool, str]:
    problems: list[str] = []
    if "route_in" in expect and actual.get("route") not in expect["route_in"]:
        problems.append(f"route={actual.get('route')} expected={expect['route_in']}")
    if "action_in" in expect and actual.get("action") not in expect["action_in"]:
        problems.append(f"action={actual.get('action')} expected={expect['action_in']}")
    answer = actual.get("answer") or ""
    for value in expect.get("answer_contains_all", []):
        if value not in answer:
            problems.append(f"answer missing {value!r}")
    for value in expect.get("answer_not_contains", []):
        if value in answer:
            problems.append(f"answer contains forbidden {value!r}")
    public_evidence = " ".join(actual.get("retrieved_evidence") or [])
    for value in expect.get("public_evidence_forbidden", []):
        if value in public_evidence:
            problems.append(f"public evidence contains {value!r}")
    if "provider_calls_max" in expect and actual.get("provider_calls", 0) > expect["provider_calls_max"]:
        problems.append(
            f"provider_calls={actual.get('provider_calls')} max={expect['provider_calls_max']}"
        )
    if "clarify_options_distinct_min" in expect:
        options = actual.get("clarify_options") or []
        labels = [o.get("label") for o in options]
        ids = [o.get("record_id") for o in options]
        need = expect["clarify_options_distinct_min"]
        if len(set(ids)) < need or len(set(labels)) < need:
            problems.append(
                f"clarify_options 구분 부족: ids={ids} labels={labels} (필요 {need})")
    return not problems, "; ".join(problems) or "target behavior satisfied"


def _run_router_case(case: dict) -> dict:
    evidence = case.get("evidence", [])
    index = build_room_entity_index(case.get("members", []), evidence)
    decision = route_query(case["query"], evidence, index)
    return {"route": decision.route, "answer": decision.answer or "", "detail": decision.detail}


def _run_output_case(case: dict) -> dict:
    evidence = case.get("evidence", [])
    index = build_room_entity_index(case.get("members", []), evidence)
    result = apply_output_gate(case["raw_answer"], case["query"], evidence, index)
    return {"action": result.action, "answer": result.answer}


def _run_chat_case(case: dict) -> dict:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    user_id = f"user-{uuid.uuid4()}"
    room_id = f"room-{uuid.uuid4()}"

    with TestSession() as db:
        db.add(User(
            id=user_id,
            google_sub=f"sub-{uuid.uuid4()}",
            username=f"ux-{uuid.uuid4()}",
            email=f"ux-{uuid.uuid4()}@example.com",
            name="테스트 사용자",
        ))
        db.add(AuthIdentity(
            id=f"identity-{uuid.uuid4()}",
            user_id=user_id,
            provider="local",
            provider_subject=f"ux-{uuid.uuid4()}",
            credential_hash="not-a-real-hash",
        ))
        db.add(FamilyRoom(
            id=room_id,
            name="UX 테스트 가족방",
            invite_code=uuid.uuid4().hex[:8].upper(),
            owner_user_id=user_id,
        ))
        db.add(FamilyMember(
            id=f"member-{uuid.uuid4()}",
            room_id=room_id,
            user_id=user_id,
            role="owner",
            relation_to_related_user=(case.get("members") or [None])[0],
        ))
        for record in case.get("records", []):
            upload = Upload(
                id=f"upload-{uuid.uuid4()}",
                room_id=room_id,
                uploader_user_id=user_id,
                type=record.get("type", "text"),
                title=record["title"],
                description=record.get("description"),
                status="uploaded",
            )
            # age_days: 최신순/관련도 케이스를 결정론으로 만들기 위한 저장 시각 지정
            if "age_days" in record:
                upload.created_at = datetime.utcnow() - timedelta(days=record["age_days"])
            db.add(upload)
        db.commit()

    provider = FixedProvider(case.get("provider_output", "기록에 있는 내용만 정리했습니다."))
    old_factory = demo_service.get_provider_for_model
    demo_service.get_provider_for_model = lambda model_id: provider
    try:
        response = demo_service.build_demo_chat_response(
            session_factory=TestSession,
            room_id=room_id,
            user_id=user_id,
            model_id="gemma-4-e2b-device",
            persona_id="father-calm",
            query=case["query"],
        )
    finally:
        demo_service.get_provider_for_model = old_factory
    engine.dispose()
    return {
        "route": response.get("gate_route"),
        "action": response.get("gate_action"),
        "answer": response.get("answer", ""),
        "retrieved_evidence": response.get("retrieved_evidence", []),
        "clarify_options": response.get("clarify_options", []),
        "provider_calls": provider.calls,
    }


def _run_case(case: dict) -> dict:
    runners = {
        "router": _run_router_case,
        "output_gate": _run_output_case,
        "chat": _run_chat_case,
    }
    return runners[case["layer"]](case)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="known_gap도 실패로 처리")
    args = parser.parse_args()

    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    baseline_failures: list[str] = []
    open_gaps: list[str] = []
    resolved_gaps: list[str] = []
    provider_calls: list[tuple[str, int]] = []
    set_stats: dict[str, dict[str, int]] = {}

    def case_set(case_id: str) -> str:
        # UX-A*: 체크포인트 8 이후 추가된 적대 확장셋. 나머지(UX-B/G)는 고정셋.
        return "적대확장셋" if case_id.startswith("UX-A") else "고정셋"

    print("Family answer UX cases")
    for case in payload["cases"]:
        actual = _run_case(case)
        ok, detail = _matches_expectation(actual, case["expect"])
        bucket = set_stats.setdefault(case_set(case["id"]),
                                      {"pass": 0, "fail": 0, "gap": 0, "resolved": 0})
        if case["layer"] == "chat":
            provider_calls.append((case["id"], actual.get("provider_calls", 0)))
        if case["status"] == "baseline":
            label = "PASS" if ok else "FAIL"
            bucket["pass" if ok else "fail"] += 1
            if not ok:
                baseline_failures.append(case["id"])
        elif ok:
            label = "RESOLVED"
            bucket["resolved"] += 1
            resolved_gaps.append(case["id"])
        else:
            label = "KNOWN GAP"
            bucket["gap"] += 1
            open_gaps.append(case["id"])
        print(f"  [{label}] {case['id']} {case['category']} - {detail}")

    print()
    for name in ("고정셋", "적대확장셋"):
        stats = set_stats.get(name)
        if stats:
            print(f"{name}: baseline {stats['pass']} PASS / {stats['fail']} FAIL, "
                  f"known_gap {stats['gap']} open / {stats['resolved']} resolved")
    if provider_calls:
        values = sorted(v for _, v in provider_calls)
        p95 = values[max(0, -(-len(values) * 95 // 100) - 1)]
        detail = ", ".join(f"{cid}={v}" for cid, v in provider_calls)
        print(f"provider calls (chat 케이스): [{detail}]")
        print(f"provider calls stats: n={len(values)} (고정+확장셋 chat 케이스 한정) "
              f"max={values[-1]} median={statistics.median(values)} p95={p95}"
              f" — 소표본 p95는 max와 같을 수 있으며 일반 트래픽 분포 추정치가 아니다")
    print("주의: 위 수치는 고정 케이스셋 계약 기준이다 — 셋 밖 일반 입력에 대한 안전성 보증이 아니다.")
    print(f"baseline failures={len(baseline_failures)} open gaps={len(open_gaps)} "
          f"resolved gaps={len(resolved_gaps)}")
    if baseline_failures or (args.strict and open_gaps):
        if baseline_failures:
            print(f"baseline failed: {', '.join(baseline_failures)}")
        if args.strict and open_gaps:
            print(f"strict open gaps: {', '.join(open_gaps)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
