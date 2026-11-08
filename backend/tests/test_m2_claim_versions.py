from dataclasses import replace

import pytest
from app.ai.grounded_answering import (
    GroundedEvidenceChanged,
    build_answer_plan,
    build_fact_sheet,
    render_plan,
)
from test_grounded_answering import chunk


@pytest.mark.parametrize("change", [
    {"active": False}, {"superseded_by": "new-version"}, {"claim_version": 999},
])
def test_inactive_or_superseded_claim_never_renders(change):
    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    stale = replace(plan, claims=(replace(plan.claims[0], **change),))
    with pytest.raises(GroundedEvidenceChanged, match="STALE_SOURCE"):
        render_plan(stale, [stale.claims[0].claim_id])


def test_claim_version_is_pinned_to_immutable_source_attempt():
    original = build_answer_plan(build_fact_sheet([chunk(source_attempt=1)]), k=1)
    revised = build_answer_plan(build_fact_sheet([chunk(source_attempt=2)]), k=1)
    assert original.claims[0].claim_version == 2
    assert revised.claims[0].claim_version == 3
    assert original.claims[0].fact_id != revised.claims[0].fact_id
