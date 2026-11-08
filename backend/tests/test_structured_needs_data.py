from datetime import date

import pytest

from app.schemas.ai import AskResponse
from app.schemas.structured_query import StructuredPlan, StructuredResult
from app.services.structured_answer import _response_route


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("FIRST_USE_NOT_RECORDED", "NEEDS_DATA"), (None, "ANSWER")],
)
def test_missing_first_use_preserves_unknown_response_state(reason, expected):
    result = StructuredResult(
        route="ANSWER",
        reason=reason,
        plan=StructuredPlan(
            operation="FILTER",
            viewer_member_id="viewer",
            family_room_id="family",
            as_of=date(2026, 10, 13),
            subject_member_ids=["subject"],
            related_event_id="receipt",
            compare_gift_use=True,
        ),
    )

    response = AskResponse(
        gate_route=_response_route(result),
        answer=None,
        citations=[],
        gate_action="structured_render",
        provider_mode="not_called",
        structured_result=result,
    ).model_dump()

    assert response["gate_route"] == expected
    assert response["structured_result"]["first_used_at"] is None
    assert response["structured_result"]["reason"] == reason
