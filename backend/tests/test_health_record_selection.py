from app.ai.health_records import HealthRecord, examination_periods, select_health_chunks
from test_grounded_answering import chunk


def test_comparison_covers_both_dates_before_duplicate_chunks():
    # Given: a long older report and a short newer report, plus another person's exam.
    records = [HealthRecord(rid, owner, (owner,), "HEALTH_EXAM", date, "건강검진", True)
               for rid, owner, date in (("old", "me", "2026-03-11"), ("new", "me", "2026-09-09"),
                                        ("other", "other", "2026-09-09"), ("year", "me", "2025-03-12"))]
    chunks = [chunk(chunk_id=f"old{i}", record_id="old", text="혈색소 12.7 페리틴 34") for i in range(6)]
    chunks += [chunk(chunk_id=rid, record_id=rid, text="혈색소 12.8 페리틴 38") for rid in ("new", "other", "year")]
    # When
    selected = select_health_chunks("2026년 9월 검사랑 3월 검사에서 혈색소랑 페리틴이 어떻게 달라졌어?",
                                    records, chunks, {"me"}, set())
    # Then
    assert {c.record_id for c in selected[:2]} == {"old", "new"}
    assert {c.record_id for c in selected} == {"old", "new"}


def test_previous_exam_uses_referenced_date_without_changing_requested_metric():
    # Given
    records = [HealthRecord(rid, "me", ("me",), "HEALTH_EXAM", date, "검진", True)
               for rid, date in (("a", "2025-09-10"), ("b", "2026-03-11"), ("c", "2026-09-09"))]
    chunks = [chunk(chunk_id=r.record_id, record_id=r.record_id, text="혈색소 페리틴") for r in records]
    # When
    selected = select_health_chunks("그 전 검사랑 혈압 수치 비교해줘", records, chunks, {"me"}, {"c"})
    # Then: absence of BP must not silently substitute older dates or another subject.
    assert [c.record_id for c in selected] == ["c", "b"]


def test_explicit_missing_date_does_not_fall_back_to_another_year():
    # Given
    record = HealthRecord("old", "me", ("me",), "HEALTH_EXAM", "2025-03-12", "검진", True)
    # When
    selected = select_health_chunks("2026년 3월 검사", [record], [chunk(record_id="old")], {"me"}, set())
    # Then
    assert selected == []


def test_periods_carry_year_only_within_current_question():
    assert examination_periods("2026년 9월 검사랑 3월 검사") == ("2026-09", "2026-03")
    assert examination_periods("2025년 9월과 2026년 3월") == ("2025-09", "2026-03")
    assert examination_periods("2026-09-09와 2026-03-11") == ("2026-09-09", "2026-03-11")


def test_explicit_anchor_with_previous_exam_keeps_both_dates():
    records = [HealthRecord(rid, "me", ("me",), "HEALTH_EXAM", date, "검진", True)
               for rid, date in (("old", "2026-03-11"), ("new", "2026-09-09"))]
    chunks = [chunk(chunk_id=r.record_id, record_id=r.record_id) for r in records]
    selected = select_health_chunks("2026년 9월 검사랑 그 전 검사 혈압 비교해줘", records, chunks, {"me"}, set())
    assert {c.record_id for c in selected} == {"old", "new"}
