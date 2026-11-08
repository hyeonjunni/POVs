"""게이트 층 단독 자가테스트 — fastapi/DB 없이 실행 가능.

    python3 backend/scripts/selftest_gates.py

DGX A/B 실험에서 검증된 케이스를 회귀 테스트로 고정한다. 실패 시 exit 1 (시끄럽게).
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.ai.gates.answer_router import route_query  # noqa: E402
from app.ai.gates.entity_index import build_room_entity_index  # noqa: E402
from app.ai.gates.output_gate import apply_output_gate  # noqa: E402

EV_BUSAN = ["부산 가족 여행 사진 메모: 2018년 여름 광안리 해변 산책, 저녁 무렵 바람이 시원했다고 남김",
            "여행 회고 메모: 바다 보면서 천천히 걷는 시간이 좋았다고 적어둠"]
EV_HOSPITAL = ["병원 예약 메모: 아버지 검진은 오전 10시, 접수는 20분 전까지 도착",
               "예약 확인 문자 OCR: 검진 예약 11:00, 10분 전 내원 요청"]
EV_PARTY = ["송년회 자리에서 아버지가 '건강하게 오래 봅시다. 사진만 남기지 말고 자주 만납시다.'라고 말함"]

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


print("① 입력 래더")
d = route_query("부산 여행의 정확한 날짜(몇 월 며칠)를 알려줘.", EV_BUSAN)
check("날짜 없음 → REFUSE (LLM 미호출)", d.route == "REFUSE", d.detail)

d = route_query("아버지 병원 예약이 몇 시였는지 알려줘.", EV_HOSPITAL)
check("시각 모순 → CONFLICT 양립 제시", d.route == "CONFLICT" and "10시" in d.answer and "11시" in d.answer)

d = route_query("부산 여행이 몇 년도였는지 알려줘.", EV_BUSAN)
check("연도 존재 → ANSWER (LLM 경로)", d.route == "ANSWER")

d = route_query("오늘 날씨 어때?", [])
check("기록 0건 → NO_RECORD", d.route == "NO_RECORD")

print("② 출력 게이트")
g = apply_output_gate("부산 여행은 2018년 7월 15일이었고 저녁은 해수 식당에서 드셨습니다.",
                      "부산 여행 언제였어?", EV_BUSAN)
check("조작 날짜(7/15) → 문장 차단·인용 강등", g.action in {"all_dropped_quote", "dropped_1"}
      and "7" not in g.answer.replace("「", ""), g.action)

g = apply_output_gate("기록에는 2018년 여름 광안리 산책만 남아 있습니다.",
                      "부산 여행 언제였어?", EV_BUSAN)
check("근거 있는 답 → 통과", g.action == "pass")

g = apply_output_gate("<think>추론중</think>아버지는 '건강하게 오래 봅시다'라고 하셨습니다.\ntags=voice",
                      "송년회에서 무슨 말 했어?", EV_PARTY)
check("think/tags 누수 제거 + 부분 인용 통과", g.leak_stripped and g.action == "pass"
      and "건강하게" in g.answer)

g = apply_output_gate("The trip was in summer 2018 at Gwangalli beach.",
                      "부산 여행 언제였어?", EV_BUSAN)
check("비한국어 응답 → 인용 강등", g.action == "fallback_quote")

print("③ 엔티티 사전 (가족방 폐쇄 세계)")
IDX = build_room_entity_index(
    member_names=["김하늘", "아버지"],
    record_texts=EV_BUSAN + EV_HOSPITAL + EV_PARTY + ["광안리해변 나들이 사진"])

d = route_query("부산에서 저녁 먹은 식당 이름이 뭐였지?", EV_BUSAN, IDX)
check("식당 이름 질문 + 이름 있는 식당 없음 → REFUSE", d.route == "REFUSE", d.detail)

d = route_query("벚꽃 구경한 공원 이름 알려줘.", ["벚꽃 아래서 찍은 가족 사진 메모"], IDX)
check("공원 이름 질문 + 공원 없음 → REFUSE", d.route == "REFUSE", d.detail)

d = route_query("예약한 병원 이름이 뭐야?", EV_HOSPITAL, IDX)
check("병원 이름 질문 + '병원' 일반명사만 → REFUSE", d.route == "REFUSE", d.detail)

d = route_query("그 사진은 누가 찍었어?", ["벚꽃 아래서 찍은 가족 사진 메모"], IDX)
check("인물 질문 + 인물 기록 없음 → REFUSE", d.route == "REFUSE", d.detail)

d = route_query("이 검진은 누구 거야?", EV_HOSPITAL, IDX)
check("인물 질문 + 아버지 존재 → ANSWER (오탐 가드)", d.route == "ANSWER")

d = route_query("부산에서 어디를 걸었어?", EV_BUSAN, IDX)
check("일반 '어디' + 해변 존재 → ANSWER (오탐 가드)", d.route == "ANSWER")

g = apply_output_gate("저녁은 광안리 근처 해운대식당에서 드셨습니다.",
                      "부산에서 저녁 뭐 먹었어?", EV_BUSAN, IDX)
check("사전에 없는 '해운대식당' → 문장 차단", g.action in {"all_dropped_quote", "dropped_1"},
      f"{g.action}/{g.unsupported_found}")

g = apply_output_gate("김하늘님이 남긴 기록에는 광안리 산책이 있습니다.",
                      "부산 기록 요약해줘", EV_BUSAN, IDX)
check("멤버 '김하늘님' → 통과 (사전 매치)", g.action == "pass", g.action)

print("④ 메타모픽/강건성 (SCPC 스위트 이식)")
# 결정성: 같은 입력 2회 → 동일 출력
d1 = route_query("막내 생일이 언제야?", EV_BUSAN + EV_HOSPITAL, IDX)
d2 = route_query("막내 생일이 언제야?", EV_BUSAN + EV_HOSPITAL, IDX)
check("결정성 (2회 동일)", (d1.route, d1.answer) == (d2.route, d2.answer))

# evidence 순서 셔플 불변
d_fwd = route_query("아버지 병원 예약이 몇 시였는지 알려줘.", EV_HOSPITAL, IDX)
d_rev = route_query("아버지 병원 예약이 몇 시였는지 알려줘.", list(reversed(EV_HOSPITAL)), IDX)
check("evidence 셔플 → route 불변", d_fwd.route == d_rev.route == "CONFLICT")

# 무관 chunk 주입 불변 (REFUSE가 흔들리지 않아야)
noisy = EV_BUSAN + ["가족방 공지: 이번 주말 대청소 예정"]
d = route_query("부산 여행의 정확한 날짜(몇 월 며칠)를 알려줘.", noisy, IDX)
check("무관 chunk 주입 → REFUSE 유지", d.route == "REFUSE")

# 무공백·메신저 변형 내성 (has_cue 이식 검증)
d = route_query("검진이 몇시였는지 알려주세여?", EV_HOSPITAL, IDX)
check("'몇시'+해요체 변형 → time 판정 유지", d.route == "CONFLICT", d.route)

# 결손 입력 생존성 (SCPC turn_index null 교훈): None 섞인 evidence에도 크래시 없이 동작
try:
    d = route_query("부산 언제 갔어?", [t for t in EV_BUSAN] + [""], IDX)
    check("빈 문자열 chunk 생존", d.route in {"ANSWER", "REFUSE", "CLARIFY"})
except Exception as exc:  # noqa: BLE001
    check("빈 문자열 chunk 생존", False, str(exc))

g1 = apply_output_gate("기록에는 2018년 여름 광안리 산책이 있습니다.", "부산 언제?", EV_BUSAN, IDX)
g2 = apply_output_gate("기록에는 2018년 여름 광안리 산책이 있습니다.", "부산 언제?",
                       list(reversed(EV_BUSAN)), IDX)
check("출력 게이트도 셔플 불변", g1.answer == g2.answer and g1.action == g2.action)

print("⑤ Phase 0 적대 회귀 — 질의값 비신뢰·전청크 모순 (NEXT_AGENT_HANDOFF)")
# (a) 질문이 심은 날짜를 모델이 복창 — 질의는 검증 대상 '주장'이지 근거가 아니다
g = apply_output_gate("부산 여행은 7월 15일이었습니다.", "부산 여행 7월 15일 맞지?", EV_BUSAN, IDX)
check("질의 전용 날짜(7/15) 복창 → 차단", g.action in {"all_dropped_quote", "dropped_1"}, g.action)

# (b) 질문이 심은 장소명 복창
g = apply_output_gate("저녁은 해운대식당에서 드셨습니다.", "해운대식당에서 먹었나?", EV_BUSAN, IDX)
check("질의 전용 장소(해운대식당) 복창 → 차단",
      g.action in {"all_dropped_quote", "dropped_1"}, g.action)

# (c) 하드원자 없는 의미 주장 — Phase 0에서 '통과 고정핀'으로 한계를 문서화했고,
#     Phase 5(UX-G12)에서 출처 주장 대조로 해소됨. 이제 차단이 기대 동작이다.
g = apply_output_gate("아버지는 막내를 가장 사랑했다고 기록되어 있습니다.",
                      "아버지는 누구를 좋아했어?", EV_PARTY, IDX)
check("의미 주장(하드원자 無) + 출처 참칭 → 차단 (Phase 5 해소)",
      g.action in {"all_dropped_quote", "dropped_1"}, g.action)

# (d) 모순 값이 3번째 청크에만 있는 경우 — 첫 2청크 비교로는 놓친다
d = route_query("아버지 검진이 몇 시였는지 알려줘.",
                ["검진 안내: 아버지 검진 오전 10시", "리마인더: 검진 10시 예정",
                 "예약 변경 문자: 검진 11:00 확정"], IDX)
check("10시/10시/11시 → CONFLICT (전 청크 대조)", d.route == "CONFLICT", d.route)

# 단일 청크 내 범위 표현('10시~11시')은 기록 간 모순이 아니다 — 오탐 가드
d = route_query("아버지 검진이 몇 시였는지 알려줘.",
                ["안내: 검진은 오전 10시에서 11시 사이 도착"], IDX)
check("단일 청크 범위 표현 → CONFLICT 아님", d.route != "CONFLICT", d.route)

print("⑥ Phase 2 타입 값 — 시각 동치·금액 동치·형식 번호 (UX-G05/06/08/09/10)")
d = route_query("검진이 몇 시야?", ["검진 안내는 오전 2시", "변경 문자는 오후 2시"], IDX)
check("오전 2시 vs 오후 2시 → 실제 모순", d.route == "CONFLICT"
      and "오전 2시" in (d.answer or "") and "오후 2시" in (d.answer or ""), d.route)

d = route_query("검진이 몇 시야?", ["검진 시간은 오후 2시", "예약 확인은 14:00"], IDX)
check("오후 2시 vs 14:00 → 같은 시각 (모순 아님)", d.route == "ANSWER", d.route)

g = apply_output_gate("1. 돼지고기를 볶습니다. 2. 김치를 익힙니다. 3. 물을 넣습니다. 4. 두부를 넣습니다.",
                      "네 단계로 정리해줘", ["돼지고기를 볶고 김치를 익힌 뒤 물과 두부를 넣는다"], IDX)
check("형식 번호 목록(1.~4.) → 통과, 4단계 유지", g.action == "pass" and "4." in g.answer, g.action)

g = apply_output_gate("가족이 3번 방문했다고 기록되어 있습니다.",
                      "몇 번 방문했어?", ["가족이 병원에 방문했다는 기록"], IDX)
check("'3번 방문' 사실 숫자 환각 → 차단 (1~3 전역허용 제거)",
      g.action in {"all_dropped_quote", "dropped_1"}, g.action)

g = apply_output_gate("용돈은 50,000원입니다.", "용돈 얼마야?", ["용돈 5만원을 준비했다"], IDX)
check("5만원 == 50,000원 동치 → 통과", g.action == "pass" and "50,000원" in g.answer, g.action)

g = apply_output_gate("용돈은 3만원입니다.", "용돈 얼마야?", ["용돈 5만원을 준비했다"], IDX)
check("금액 환각(3만원≠5만원) → 차단", g.action in {"all_dropped_quote", "dropped_1"}, g.action)

print("⑦ Phase 3 사건 스코프 모순·모호 주어 (UX-G04/07/13/15)")
IDX2 = build_room_entity_index(["아버지", "어머니", "막내"], [])
d = route_query("가족 병원 예약이 몇 시야?",
                ["아버지 치과 예약은 오전 10시", "어머니 안과 예약은 오전 11시"], IDX2)
check("다른 인물의 일정 → 모순 아님 (CLARIFY/ANSWER)", d.route in {"CLARIFY", "ANSWER"}, d.route)

d = route_query("아버지 검진이 몇 시야?",
                ["아버지 검진은 9시", "아버지 검진은 10시", "아버지 검진은 11시"], IDX2)
check("같은 인물 3자 모순 → 세 값 모두 표시", d.route == "CONFLICT"
      and all(v in (d.answer or "") for v in ("9시", "10시", "11시")), d.answer)

d = route_query("생일이 언제야?", ["아버지 생일은 3월 1일", "막내 생일은 5월 3일"], IDX2)
check("주어 없는 질문 + 두 인물 기록 → CLARIFY (임의 선택 금지)",
      d.route == "CLARIFY" and d.detail == "ambiguous_person"
      and "아버지" in (d.answer or "") and "막내" in (d.answer or ""), d.route)

d = route_query("막내 생일이 언제야?", ["아버지 생일은 3월 1일", "막내 생일은 5월 3일"], IDX2)
check("주어 명시(막내) → ANSWER (과잉 CLARIFY 금지)", d.route == "ANSWER", d.route)

d = route_query("검진 도착 시간이 몇 시야?",
                ["검진은 오전 10시에서 11시 사이 도착", "접수 안내는 오전 10시부터 시작"], IDX2)
check("범위 기록 + 범위 안 확인값 → 모순 아님", d.route == "ANSWER", d.route)

d = route_query("검진 도착 시간이 몇 시야?",
                ["검진은 오전 10시에서 11시 사이 도착", "변경 안내는 오후 1시 도착"], IDX2)
check("범위 밖 점값 → 실제 모순 유지", d.route == "CONFLICT", d.route)

print("⑧ Phase 4 가족 어휘·장소 띄어쓰기·부분답변 발췌 (UX-G01/03/14)")
IDX3 = build_room_entity_index(["아버지"], ["아버지 예약 병원은 세브란스 병원으로 적혀 있음"])
d = route_query("예약한 병원 이름이 뭐야?",
                ["아버지 예약 병원은 세브란스 병원으로 적혀 있음"], IDX3)
check("띄어 쓴 장소명(세브란스 병원) → ANSWER", d.route == "ANSWER", d.route)

d = route_query("예약한 병원 이름이 뭐야?", EV_HOSPITAL, IDX)
check("이름 없는 '병원' 언급만 → REFUSE 유지 (오탐 가드)", d.route == "REFUSE", d.detail)

g = apply_output_gate("예약된 곳은 세브란스병원입니다.", "병원 이름 알려줘",
                      ["아버지 예약 병원은 세브란스 병원으로 적혀 있음"], IDX3)
check("띄어쓰기 변형 복창(세브란스병원) → 통과", g.action == "pass", g.action)

from app.ai.gates.textrules import evidence_excerpt, fold_family_terms  # noqa: E402
check("가족 호칭 접기 (어머니 생신 → 어머니 생일)",
      fold_family_terms("엄마 생신") == "어머니 생일")
long_record = ("가족들이 오랜만에 함께 모여 바닷가를 걷고 사진을 찍으며 저녁까지 시간을 "
               "보냈다는 긴 회상 기록이며 시기는 2018년 여름으로 남아 있음")
check("긴 기록 발췌에 핵심 값(2018년 여름) 보존",
      "2018년 여름" in evidence_excerpt(long_record, "부산 여행이 몇 월 며칠이야?"))
check("짧은 기록은 전문 유지", evidence_excerpt("막내 생일은 5월 3일", "") == "막내 생일은 5월 3일")

print("⑨ Phase 5 출처 주장 대조 — 감정·관계 환각 (UX-G12)")
IDX5 = build_room_entity_index(["아버지", "막내"], ["송년회에서 아버지가 가족사진을 남김"])
g = apply_output_gate("아버지는 막내를 가장 사랑했다고 기록되어 있습니다.",
                      "아버지는 막내를 어떻게 생각했어?",
                      ["송년회에서 아버지가 가족사진을 남김"], IDX5)
check("무근거 감정·관계 + '기록되어 있다' 참칭 → 차단",
      g.action in {"all_dropped_quote", "dropped_1"}, g.action)

g = apply_output_gate("송년회에서 가족사진을 남겼다고 기록되어 있습니다.",
                      "송년회에 뭐 했어?", ["송년회에서 아버지가 가족사진을 남김"], IDX5)
check("출처 주장 + 내용 접점 있음 → 통과", g.action == "pass", g.action)

g = apply_output_gate("아버지의 가족 사랑이 느껴지는 날이었기를 바랍니다.",
                      "덕담 한마디 써줘", ["송년회에서 아버지가 가족사진을 남김"], IDX5)
check("출처 주장 없는 덕담·감상 → 통과 (창작 허용 정책선)", g.action == "pass", g.action)

g = apply_output_gate("<think>추론</think>아버지는 '건강하게 오래 봅시다'라고 하셨습니다.",
                      "송년회에서 무슨 말 했어?", EV_PARTY, IDX)
check("실제 인용 '라고 하셨' → 통과 유지", g.action == "pass" and "건강하게" in g.answer, g.action)

print("⑩ 체크포인트 8 감사 — 감정 단정·부정 반전·재작성 가드·사건 스코프 (UX-A01~A08)")
IDX10 = build_room_entity_index(["아버지", "막내"], ["송년회에서 아버지가 가족사진을 남김"])
g = apply_output_gate("아버지는 막내를 가장 사랑했어요.", "아버지는 막내를 어떻게 생각했어?",
                      ["송년회에서 아버지가 가족사진을 남김"], IDX10)
check("무출처 감정 단정 → 차단 (A01)", g.action in {"all_dropped_quote", "dropped_1"}, g.action)

g = apply_output_gate("아버지는 가족사진을 보며 막내를 가장 사랑했다고 기록되어 있습니다.",
                      "아버지는 막내를 어떻게 생각했어?",
                      ["송년회에서 아버지가 가족사진을 남김"], IDX10)
check("기록 단어 끼워넣기+출처 참칭 → 차단 (A02)",
      g.action in {"all_dropped_quote", "dropped_1"}, g.action)

EV_LOVE = ["아버지는 막내를 사랑한다고 기록되어 있습니다"]
g = apply_output_gate("아버지는 막내를 사랑하지 않았다고 기록되어 있습니다.",
                      "아버지는 막내를 어떻게 생각했어?", EV_LOVE, IDX10)
check("부정 반전 → 차단 (A03)", g.action in {"all_dropped_quote", "dropped_1"}, g.action)

g = apply_output_gate("아버지는 막내를 사랑한다고 기록되어 있습니다.",
                      "아버지는 막내를 어떻게 생각했어?", EV_LOVE, IDX10)
check("기록이 지지하는 감정 서술 → 통과 (A04 오탐 가드)", g.action == "pass", g.action)

from app.ai.gates.answer_router import rewrite_preserves_intent  # noqa: E402
check("재작성 가드: 표기 복원 채택 (몇일→며칠)",
      rewrite_preserves_intent("막내 생일이 몇일이야?", "막내 생일이 며칠이야?", IDX10))
check("재작성 가드: 인물 추가 폐기",
      not rewrite_preserves_intent("그때 그거 언제였지?", "막내 생일이 언제였지?", IDX10))
check("재작성 가드: 숫자 추가 폐기",
      not rewrite_preserves_intent("부산 언제 갔어?", "부산 7월 15일에 갔어?", IDX10))
check("재작성 가드: 부정 반전 폐기",
      not rewrite_preserves_intent("검진 갔어?", "검진 안 갔어?", IDX10))
check("재작성 가드: 질문 종류 변경 폐기",
      not rewrite_preserves_intent("검진 언제야?", "검진 몇 시야?", IDX10))

EV_TWO_EVENTS = ["아버지 치과 예약은 오전 10시", "아버지 건강검진 예약은 오전 11시"]
d = route_query("아버지 예약이 몇 시야?", EV_TWO_EVENTS, IDX2)
check("같은 인물·다른 사건 + 사건 미특정 → CLARIFY (A06)",
      d.route == "CLARIFY" and d.detail == "ambiguous_event"
      and "치과" in (d.answer or "") and "건강검진" in (d.answer or ""), d.route)

d = route_query("아버지 치과 예약이 몇 시야?", EV_TWO_EVENTS, IDX2)
check("사건 특정 → ANSWER (A07)", d.route == "ANSWER", d.route)

d = route_query("아버지 검진이 몇 시야?",
                ["아버지 검진은 오전 10시", "아버지 건강검진 확인 문자는 11:00"], IDX2)
check("검진↔건강검진(포함 관계)은 같은 사건 → 실모순 유지", d.route == "CONFLICT", d.route)

print("⑪ 체크포인트 9 감사 — 절 분리 감정·재작성 하이재킹·사건 시그니처 (UX-A09~A14)")
IDX11 = build_room_entity_index(["아버지", "막내"], ["송년회에서 아버지가 가족사진을 남김"])
EV_PHOTO = ["송년회에서 아버지가 가족사진을 남김"]
g = apply_output_gate("아버지는 막내를 가장 사랑했고 늘 응원했어요.",
                      "아버지는 막내를 어떻게 생각했어?", EV_PHOTO, IDX11)
check("혼합 절(사실+기원) → 사실 절 무근거로 차단 (A09)",
      g.action in {"all_dropped_quote", "dropped_1"}, g.action)

g = apply_output_gate("아버지가 막내를 가장 사랑했으니 행복하길 바랍니다.",
                      "아버지는 막내를 어떻게 생각했어?", EV_PHOTO, IDX11)
check("덕담 꼬리 밀반입 → 사실 절 차단 (A10)",
      g.action in {"all_dropped_quote", "dropped_1"}, g.action)

g = apply_output_gate("아버지가 늘 건강하고 행복하시길 바랍니다.",
                      "덕담 한마디 써줘", EV_PHOTO, IDX11)
check("순수 기원문(사실 절 없음) → 통과 (과잉거절 가드)", g.action == "pass", g.action)

check("주제 하이재킹(부산 여행) 폐기",
      not rewrite_preserves_intent("그때 그건 언제였어?", "부산 여행은 언제였어?", IDX11))
check("주제 하이재킹(장례식) 폐기",
      not rewrite_preserves_intent("그때 그건 언제였어?", "장례식은 언제였어?", IDX11))
check("의문사 부류 변경(왜→어떻게) 폐기",
      not rewrite_preserves_intent("왜 그랬어?", "어떻게 했어?", IDX11))

d = route_query("아버지 검진이 몇 시야?",
                ["아버지 종합검진 예약은 오전 10시", "아버지 건강검사 안내는 오전 11시"], IDX2)
check("종합검진↔건강검사(같은 시그니처) → 실모순 (A13)", d.route == "CONFLICT"
      and "10시" in (d.answer or "") and "11시" in (d.answer or ""), d.route)

d = route_query("아버지 검진이 몇 시야?",
                ["아버지 치과 검진 예약은 오전 10시", "아버지 건강 검진 안내는 오전 11시"], IDX2)
check("치과 검진↔건강 검진(진료과 한쪽만) → 거짓 모순 대신 CLARIFY (A14)",
      d.route == "CLARIFY" and d.detail == "ambiguous_event", d.route)

d = route_query("아버지 치과 검진이 몇 시야?",
                ["아버지 치과 검진 예약은 오전 10시", "아버지 건강 검진 안내는 오전 11시"], IDX2)
check("진료과 명시 질의 → 특정 ANSWER 유지", d.route == "ANSWER", d.route)

d = route_query("아버지 예약이 몇 시야?",
                ["아버지 치과 예약은 오전 10시", "아버지 안과 예약은 오전 11시"], IDX2)
check("치과↔안과(진료과 상이) → 모순 아님 (different)", d.route != "CONFLICT", d.route)

print("⑫ 체크포인트 10 감사 — 술어 양태·미지 사건·질의 스코프 (UX-A15~A20)")
EV_PHOTO10 = ["송년회에서 아버지가 가족사진을 남김"]
g = apply_output_gate("아버지는 막내를 응원했어요.", "아버지는 막내를 어떻게 생각했어?",
                      EV_PHOTO10, IDX11)
check("'응원했어요' 과거 사실형 → 차단 (A15)", g.action in {"all_dropped_quote", "dropped_1"}, g.action)
g = apply_output_gate("아버지는 막내를 축하했어요.", "아버지는 막내를 어떻게 생각했어?",
                      EV_PHOTO10, IDX11)
check("'축하했어요' 과거 사실형 → 차단 (A16)", g.action in {"all_dropped_quote", "dropped_1"}, g.action)
g = apply_output_gate("막내를 진심으로 축하드립니다.", "축하 인사 써줘", EV_PHOTO10, IDX11)
check("'축하드립니다' 수행형 → 통과 (양태 가드)", g.action == "pass", g.action)
g = apply_output_gate("막내를 항상 응원할게요.", "응원 한마디 써줘", EV_PHOTO10, IDX11)
check("'응원할게요' 수행형 → 통과 (양태 가드)", g.action == "pass", g.action)

# 메타모픽: 쉼표·붙여쓰기·연결어미 변형에도 동일 차단 (표기 의존 우회 금지)
for variant in ["아버지는 막내를 가장 사랑했고, 늘 응원했어요.",
                "아버지는 막내를 가장 사랑했고늘 응원했어요.",
                "아버지는 막내를 가장 사랑했는데 늘 응원했어요."]:
    g = apply_output_gate(variant, "아버지는 막내를 어떻게 생각했어?", EV_PHOTO10, IDX11)
    check(f"표기 변형 동일 차단: {variant[12:24]}…",
          g.action in {"all_dropped_quote", "dropped_1"}, g.action)

d = route_query("아버지 모임이 몇 시야?",
                ["아버지 낚시 모임은 오전 10시", "아버지 독서 모임은 오전 11시"], IDX2)
check("사전 밖 사건 둘(낚시/독서) → 모순 아님·보수 CLARIFY (A18)",
      d.route == "CLARIFY" and d.detail == "ambiguous_event", d.route)
d = route_query("아버지 일정이 몇 시야?",
                ["아버지 가족 여행 출발은 오전 10시", "아버지 독서 모임은 오전 11시"], IDX2)
check("식별 사건(여행) vs 미지 사건(독서 모임) → 모순 아님", d.route != "CONFLICT", d.route)

EV_SCOPE = ["아버지 여행 출발은 오전 10시", "아버지 생일 모임은 오전 11시"]
d = route_query("아버지 여행은 몇 시야?", EV_SCOPE, IDX2)
check("질의 사건(여행) 스코프 → ANSWER (A19)", d.route == "ANSWER", d.route)
d = route_query("아버지 생일은 몇 시야?", EV_SCOPE, IDX2)
check("질의 사건(생일) 스코프 → ANSWER", d.route == "ANSWER", d.route)

d = route_query("아버지 생일은 몇 시야?",
                ["아버지 여행 출발은 오전 10시", "아버지 여행 복귀는 오전 11시",
                 "아버지 생일 모임은 오전 9시"], IDX2)
check("무관 사건(여행 10/11시) 모순에 안 끌려감 → 생일 ANSWER", d.route == "ANSWER", d.route)

print()
if failures:
    print(f"❌ 실패 {len(failures)}건: {failures}")
    raise SystemExit(1)
print("✅ 게이트 자가테스트 전부 통과")
