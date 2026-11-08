import pytest

from app.ai.travel_events import TravelEvent, select_travel_events, summarize_travel_events


def event(event_id: str, date: str = "2025-01-01") -> TravelEvent:
    return {"event_id": event_id, "title": "함께 떠난 여행", "category": "TRIP",
            "date": date, "end": None, "precision": "EXACT", "location": "여행지",
            "participants": [{"name": "질문자", "modes": ["ONSITE"]},
                             {"name": "엄마", "modes": ["ONSITE"]}],
            "record_ids": [event_id]}


@pytest.mark.parametrize("participants", [[], [{"name": "엄마", "modes": ["ONSITE"]}],
    [{"name": "엄마", "modes": ["ONSITE"]}, {"name": "질문자", "modes": ["REMOTE"]}]])
def test_together_requires_both_people_confirmed_onsite(participants):
    # Given a trip whose attendance does not establish the requested pair.
    candidate = event("trip")
    candidate["participants"] = participants
    # When counting their shared travels.
    selected = select_travel_events([candidate], "엄마랑 여행", {"질문자", "엄마"})
    # Then a name mention or remote participation cannot establish attendance.
    assert selected == []


def test_same_event_counts_once_and_keeps_its_linked_photo_sources():
    # Given an event and two linked records with no separate attendance evidence.
    source = event("trip")
    photo = {**source, "record_ids": ["photo"], "participants": []}
    diary = {**source, "record_ids": ["diary"], "participants": []}
    # When selecting and counting.
    result = summarize_travel_events(select_travel_events(
        [photo, source, diary, source], "엄마랑 여행 몇 번", {"질문자", "엄마"}))
    # Then duplicate media do not inflate the count and remain traceable.
    assert result["count"] == 1
    assert "여행 1회, 나들이 0회" in result["answer"]
    assert result["event_ids"] == ["trip"]
    assert result["record_ids"] == ["photo", "trip", "diary"]


def test_verified_outing_included_but_home_event_excluded():
    # Given two OTHER events with confirmed attendance.
    outing = {**event("outing"), "category": "OTHER", "location": "공원 산책로"}
    home = {**event("home"), "category": "OTHER", "title": "앨범 정리", "location": "집"}
    # When selecting travel including outings.
    result = select_travel_events([home, outing], "여행", {"질문자", "엄마"})
    # Then classification needs actual outing evidence.
    assert [item["event_id"] for item in result] == ["outing"]


@pytest.mark.parametrize("operation,expected", [("earliest", "old"), ("latest", "recent")])
def test_first_and_last_compare_approximate_years_on_same_date_scale(operation, expected):
    # Given chronological records including an approximate middle date.
    old = event("old", "2009-05-17")
    approximate = {**event("middle", "2011년 여름"), "precision": "APPROXIMATE"}
    recent = event("recent", "2025-08-15")
    # When selecting an extremum from unsorted events.
    result = summarize_travel_events([recent, approximate, old], operation)
    # Then only the chosen event can supply the follow-up photos.
    assert result["event_ids"] == [expected]
    assert result["record_ids"] == [expected]


def test_missing_date_counts_but_cannot_be_claimed_first_or_latest():
    # Given a confirmed trip without an event date.
    candidate = {**event("undated"), "date": None}
    # When asking for its temporal position.
    result = summarize_travel_events([candidate], "latest")
    # Then it remains counted but no unsupported ordering or source is selected.
    assert result["count"] == 1
    assert result["event_ids"] == []
    assert "찾지 못했습니다" in result["answer"]


@pytest.mark.parametrize("tag,expected", [("여행", 1), ("나들이", 1), ("여행갈계획", 0)])
def test_explicit_tags_support_classification_without_substring_guessing(tag, expected):
    candidate = {**event("tagged"), "category": "OTHER", "title": "함께한 하루",
                 "location": "기록된 장소", "tags": [tag]}
    result = select_travel_events([candidate], "여행", {"질문자", "엄마"})
    assert len(result) == expected
    if result:
        assert result[0]["travel_evidence"] == "tags:" + tag


def test_count_names_trips_and_outings_on_one_consistent_scope():
    trip = event("trip")
    outing = {**event("outing"), "category": "OTHER", "title": "공원 산책"}
    result = summarize_travel_events(select_travel_events([trip, outing], "여행", {"질문자", "엄마"}))
    assert result["count"] == 2
    assert "여행·나들이는 2회" in result["answer"]
    assert "여행 1회, 나들이 1회" in result["answer"]
