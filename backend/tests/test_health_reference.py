from app.ai.health_reference import select_health_references


def test_anemia_food_advice_does_not_require_a_family_record():
    references = select_health_references("나 빈혈 있을 때 뭘 먹어야 돼?")
    assert len(references) == 1
    assert references[0].condition == "빈혈"
    assert references[0].source_url == "https://www.nhs.uk/conditions/iron-deficiency-anaemia/"
    assert "철결핍이라고 확정할 수 없다" in references[0].summary
    assert "콩" in references[0].summary


def test_explicit_condition_wins_over_previous_topic():
    references = select_health_references("고혈압일 때 좋은 음식 알려줘", "이전 질문: 저혈압과 빈혈")
    assert [ref.condition for ref in references] == ["고혈압"]


def test_followup_uses_only_provided_context():
    assert not select_health_references("그럼 뭘 먹는 게 좋아?")
    references = select_health_references("그럼 뭘 먹는 게 좋아?", "이번 검진에 빈혈 소견이 있었다.")
    assert [ref.condition for ref in references] == ["빈혈"]


def test_record_lookup_and_comparison_are_not_advice():
    for question in ("내 빈혈 기록 보여줘", "저혈압 수치가 전보다 얼마나 달라졌어?",
                     "빈혈 있을 때 내가 어떤 음식을 먹었어?", "고혈압 진단은 언제 받았어?"):
        assert not select_health_references(question)


def test_supported_conditions_remain_separate_and_unknown_topics_do_not_guess():
    references = select_health_references("고혈압하고 저혈압은 식단 관리를 어떻게 해?")
    assert {ref.condition for ref in references} == {"고혈압", "저혈압"}
    assert all(ref.source_url.startswith("https://www.nhs.uk/") for ref in references)
    assert not select_health_references("여행지 음식 추천해줘")
    assert not select_health_references("당뇨 식단 추천해줘")


def test_caution_wording_selects_the_explicit_condition_only():
    from app.ai.health_reference import select_health_references
    assert [ref.condition for ref in select_health_references("저혈압에는 뭘 조심해야 돼요?")] == ["저혈압"]
    assert [ref.condition for ref in select_health_references("고혈압이면 피해야 할 음식 있어?")] == ["고혈압"]
    assert select_health_references("엄마 혈압 기록 조심스럽게 보여줘", "기록 없음") == ()
