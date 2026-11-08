"""게이트 원커맨드 CI — SCPC check.py 이식. 커밋 전 한 방에 전부 검증.

    python3 backend/scripts/check_gates.py

1. selftest_gates (유닛 16케이스 + 메타모픽 스위트)
2. 골든셋 30문항 라우팅 회귀 (backend/tests/golden_set.json)
3. E2E 실기동 (FastAPI TestClient 전 경로)
종료 코드: 0 통과, 1 실패 (성공 조용, 실패 요란).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.ai.gates.answer_router import route_query  # noqa: E402
from app.ai.gates.entity_index import build_room_entity_index  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def run_script(label: str, script: str) -> None:
    # Force UTF-8 on both ends of the pipe (-X utf8 for the child, explicit
    # decode for the parent) — Windows' default locale codepage otherwise
    # breaks decoding the child's Korean stdout/stderr (Task 5 encoding fix).
    proc = subprocess.run([sys.executable, "-X", "utf8", str(BACKEND / "scripts" / script)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    last = (proc.stdout.strip().splitlines() or ["?"])[-1]
    if proc.returncode != 0:
        # 실패 원인은 대개 stderr(트레이스백)에 있다 — '?'만 보여주지 않는다 (Phase 0)
        err_tail = " | ".join(proc.stderr.strip().splitlines()[-3:])
        if err_tail:
            last = f"{last} :: {err_tail}"
    check(label, proc.returncode == 0, last)


def run_pytest(label: str, *paths: str, deferred: bool = False) -> None:
    env = os.environ.copy()
    # Local gates must never turn a configured developer URL into unapproved
    # PostgreSQL evidence; live pgvector execution has its own human gate.
    env["AMBIENT_TEST_POSTGRES_URL"] = ""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", *paths, "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    last = (proc.stdout.strip().splitlines() or ["?"])[-1]
    if proc.returncode != 0:
        check(label, False, last)
    elif deferred:
        print(f"  [DEFERRED] {label} — {last}")
    else:
        check(label, True, last)


print("1) 유닛 + 메타모픽")
run_script("selftest_gates", "selftest_gates.py")

print("2) 골든셋 라우팅 회귀")
golden = json.loads((BACKEND / "tests" / "golden_set.json").read_text(encoding="utf-8"))
routes = {}
label_route = {}
for item in golden["items"]:
    texts = [c["text"] for c in golden["rooms"][item["room"]]]
    index = build_room_entity_index(golden["members"], texts)
    decision = route_query(item["q"], texts, index)
    routes[decision.route] = routes.get(decision.route, 0) + 1
    label_route.setdefault(item["label"], {}).setdefault(decision.route, 0)
    label_route[item["label"]][decision.route] += 1

check("answerable 10/10 → ANSWER (라우팅 과잉거절 0)",
      label_route.get("answerable", {}).get("ANSWER", 0) == 10,
      str(label_route.get("answerable")))
check("unanswerable 12/13 이상 → REFUSE",
      label_route.get("unanswerable", {}).get("REFUSE", 0) >= 12,
      str(label_route.get("unanswerable")))
check("conflict 2/2 → CONFLICT",
      label_route.get("conflict", {}).get("CONFLICT", 0) == 2,
      str(label_route.get("conflict")))
check("advice 5/5 → ANSWER",
      label_route.get("advice", {}).get("ANSWER", 0) == 5,
      str(label_route.get("advice")))

print("3) E2E 실기동 (FastAPI 전 경로)")
run_script("e2e_gate_demo", "e2e_gate_demo.py")

print("4) M2 검색·마이그레이션·평가 (SQLite)")
run_pytest(
    "M2 retrieval / SQLite migration suite",
    "tests/test_retrieval_chunking.py",
    "tests/test_retrieval_ranking.py",
    "tests/test_retrieval_indexing.py",
    "tests/test_retrieval_api.py",
    "tests/test_retrieval_evaluation.py",
    "tests/test_migrations.py",
)
run_pytest(
    "PostgreSQL/pgvector execution (human approval required)",
    "tests/test_retrieval_postgres.py",
    deferred=True,
)

print("5) M3 source-bound answering (fake providers)")
run_pytest(
    "M3 grounded answering / transaction boundary",
    "tests/test_grounded_answering.py",
    "tests/test_grounded_answering_api.py",
    "tests/test_transaction_boundary.py",
)

print()
if failures:
    print(f"❌ 실패 {len(failures)}건: {failures}")
    raise SystemExit(1)
print("✅ check_gates 전부 통과")
