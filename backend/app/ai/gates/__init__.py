"""규칙 게이트 층 — LLM은 지각, 규칙이 제어흐름 (HALLUCINATION_GATE_PLAN.md Phase 1·2).

순수 stdlib. 앱 모델/DB에 의존하지 않아 단독 테스트 가능:
    python3 backend/scripts/selftest_gates.py
"""
from app.ai.gates.answer_router import RouteDecision, route_query  # noqa: F401
from app.ai.gates.output_gate import GateResult, apply_output_gate  # noqa: F401
