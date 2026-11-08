import pytest

from app.ai.gates.answer_router import _entity_answerable
from app.ai.gates.entity_index import RoomEntityIndex


@pytest.mark.parametrize(
    ("query", "evidence"),
    [
        ("어디를 걸었어?", "우리는 강릉 바다를 함께 걸었다."),
        ("어디를 걸었어?", "우리는 광안리 해변을 함께 걸었다."),
        ("어디에서 사진을 찍었어?", "서울 공원에서 사진을 찍었다."),
        ("어디에 갔어?", "가족과 병원에 갔다."),
        ("어디에 갔어?", "가족과 절에 갔다."),
        ("어디에서 찍었어?", "절에서 사진을 찍었다."),
        ("어디에 갔어?", "병원에도 갔다."),
        ("어디에서 찍었어?", "공원에서는 사진을 찍었다."),
        ("어디에서 만났어?", "학교에서만 만났다."),
    ],
)
def test_generic_place_is_answerable_from_grounded_place_evidence(query, evidence):
    assert _entity_answerable(query, [evidence], RoomEntityIndex()) == (True, "generic_place")


@pytest.mark.parametrize(
    "evidence",
    [
        "가족 사진을 함께 보았다.",
        "가족과 바다 사진을 함께 보았다.",
        "공원 사진을 정리했다.",
        "가족과 바다를 찍은 사진을 함께 보았다.",
        "가족과 바다를 찍은 사진을 함께 보았다. 저녁에는 산책했다.",
        "바다를 찍었다\n산책했다.",
        "바다를 찍었다: 산책했다.",
        "바다를 찍었다 / 산책했다.",
        "가족과 바다를 찍은 사진을 걸어 두었다.",
        "가족과 바다를 찍은 사진, 이걸 함께 보았다.",
        "바다를 찍은 사진을 보았다. 비 때문에 일정이 걸렸다.",
        "걸포공원을 찍은 사진을 보았다.",
        "바다 사진에서는 파도가 보였다.",
        "바다 영상만은 함께 보았다.",
        "바다 기록에도 날짜가 없다.",
        "가족과 바다를 찍은 산책 사진을 보았다.",
        "가족과 바다를 찍은 걷기 사진을 보았다.",
        "가족과 바다를 찍은 지나 사진을 보았다.",
        "검진 시간은 오후 2시였다.",
    ],
)
def test_generic_place_stays_fail_closed_without_location_evidence(evidence):
    assert _entity_answerable("어디에서 봤어?", [evidence], RoomEntityIndex()) == (
        False,
        "generic_place",
    )
