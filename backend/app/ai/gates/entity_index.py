"""가족방 엔티티 사전 (Phase 1 확장) — 닫힌 세계의 고유명사 근거.

이 서비스의 구조적 이점: 정답(가족 기록·멤버)이 전부 우리 DB에 있다.
- 인물: FamilyMember⋈User 의 이름 + 관계 호칭 (DB에서 주입)
- 장소: 업로드 제목/태그/본문에서 접미사 규칙으로 추출 (X식당, X병원 …)
특정 가족의 데이터는 코드에 박지 않는다 — 규칙은 코드, 사전은 데이터.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import re

from app.ai.gates.textrules import JOSA, STOP, content_tokens, strip_josa

# 도메인 정적 어휘 (가족 유산 서비스라 안 낡는 하드코딩 — SCPC cue 리스트와 같은 부류)
PLACE_SUFFIXES = ("식당", "병원", "공원", "카페", "호텔", "펜션", "가게", "센터", "시장",
                  "해변", "바다", "학교", "숙소", "약국", "미용실", "성당", "교회", "절")
KINSHIP = ("아버지", "아빠", "엄마", "어머니", "할아버지", "할머니", "외할아버지", "외할머니",
           "막내", "첫째", "둘째", "셋째", "형", "누나", "오빠", "언니", "동생", "이모",
           "삼촌", "고모", "외삼촌", "사촌", "아들", "딸", "손자", "손녀")

# 질문에서 장소 카테고리를 특정하는 명사 → 해당 카테고리(동의어 포함)의 '이름 있는' 장소가
# 기록/사전에 있어야 답변 가능으로 본다.
PLACE_CATEGORY_SYNONYMS = {
    "식당": ("식당", "맛집", "음식점"),
    "가게": ("가게", "상점", "매장"),
    "병원": ("병원", "의원", "클리닉"),
    "공원": ("공원",),
    "숙소": ("숙소", "호텔", "펜션", "리조트"),
    "카페": ("카페",),
    "학교": ("학교",),
    "시장": ("시장",),
}


@dataclass
class RoomEntityIndex:
    persons: set[str] = field(default_factory=set)   # 멤버 이름 + 관계 호칭
    places: set[str] = field(default_factory=set)    # 이름 있는 장소 토큰 (예: 광안리해변 X — 접미사형)


# 접미사 앞에 와도 '이름'이 되지 못하는 일반 수식어 — "예약 병원"은 이름 있는 병원이 아니다
GENERIC_PLACE_MODIFIERS = ("예약", "방문", "근처", "동네", "우리", "가족", "단골", "지난",
                           "이번", "다음", "그날", "당일")
PLACE_ARTIFACT_NOUNS = frozenset({"사진", "영상", "기록"})
LOCATION_JOSA = ("에서", "으로", "에", "로")
OBJECT_JOSA = ("을", "를")
TRAILING_JOSA = ("도", "는", "은", "만")
MOVEMENT_FORMS = frozenset({"걷다", "걸었다", "산책하다", "산책했다", "지나다", "지나갔다"})


def _place_token_and_particle(raw: str) -> tuple[str, str | None]:
    core = raw
    while True:
        stripped = False
        for josa in TRAILING_JOSA:
            if core.endswith(josa) and len(core) > len(josa):
                core = core[:-len(josa)]
                stripped = True
                break
        if not stripped:
            break
    for kind, particles in (("location", LOCATION_JOSA), ("object", OBJECT_JOSA)):
        for particle in particles:
            if core.endswith(particle) and len(core) > len(particle):
                return core[:-len(particle)], kind
    for particle in sorted(JOSA, key=len, reverse=True):
        if core.endswith(particle) and len(core) > len(particle):
            return core[:-len(particle)], None
    return core, None


def has_grounded_place_evidence(texts: list[str]) -> bool:
    """장소 조사는 인정하고, 목적격은 이동 문맥일 때만 장소로 인정한다."""
    for text in texts:
        for clause in re.split(r"[.!?;,\r\n:/|]+", text or ""):
            raw_tokens = re.sub(r"[^\w가-힣\s]", " ", clause).split()
            tokens = [_place_token_and_particle(raw)[0] for raw in raw_tokens]
            for index, raw in enumerate(raw_tokens):
                token, particle_kind = _place_token_and_particle(raw)
                if not any(token.endswith(suffix) for suffix in PLACE_SUFFIXES):
                    continue
                next_token = tokens[index + 1] if index + 1 < len(tokens) else None
                if particle_kind == "location":
                    return True
                if particle_kind == "object":
                    following = raw_tokens[index + 1:index + 3]
                    if any(word in MOVEMENT_FORMS for word in following):
                        return True
                    continue
                if raw == token and next_token in PLACE_ARTIFACT_NOUNS:
                    continue
                return True
    return False


def extract_named_places(texts: list[str]) -> set[str]:
    """'이름 있는' 장소 추출 — 붙여 쓴 형(광안리해변)과 띄어 쓴 형(세브란스 병원) 모두.

    '병원' 단독이나 '예약 병원'처럼 이름 없는 언급은 장소명이 아니다 (UX-G03).
    띄어 쓴 이름은 원형과 무공백형을 함께 등록해 출력 게이트 대조도 통과시킨다.
    """
    places: set[str] = set()
    for text in texts:
        for token in content_tokens(text):
            for suffix in PLACE_SUFFIXES:
                if token.endswith(suffix) and len(token) > len(suffix):
                    places.add(token)
        ordered = [strip_josa(raw) for raw in re.sub(r"[^\w가-힣\s]", " ", text or "").split()]
        for prev, cur in zip(ordered, ordered[1:]):
            if (cur in PLACE_SUFFIXES and len(prev) >= 2 and not prev.isdigit()
                    and prev not in STOP and prev not in KINSHIP
                    and prev not in GENERIC_PLACE_MODIFIERS
                    and not any(prev.endswith(s) for s in PLACE_SUFFIXES)):
                places.add(f"{prev} {cur}")
                places.add(f"{prev}{cur}")
    return places


def extract_kinship_terms(texts: list[str]) -> set[str]:
    found: set[str] = set()
    joined = " ".join(texts)
    for term in KINSHIP:
        if term in joined:
            found.add(term)
    return found


def build_room_entity_index(member_names: list[str], record_texts: list[str]) -> RoomEntityIndex:
    persons = {name.strip() for name in member_names if name and len(name.strip()) >= 2}
    persons |= extract_kinship_terms(record_texts)
    places = extract_named_places(record_texts)
    return RoomEntityIndex(persons=persons, places=places)


def question_place_category(query: str) -> tuple[str, ...] | None:
    """질문이 특정 장소 카테고리의 '이름'을 묻는가 → 동의어 묶음 반환."""
    for synonyms in PLACE_CATEGORY_SYNONYMS.values():
        if any(word in query for word in synonyms):
            return synonyms
    return None


def has_named_place_of(category_synonyms: tuple[str, ...], index: RoomEntityIndex,
                       evidence_texts: list[str]) -> bool:
    candidates = index.places | extract_named_places(evidence_texts)
    return any(place.endswith(suffix) for place in candidates for suffix in category_synonyms)


def has_any_person(index: RoomEntityIndex, evidence_texts: list[str]) -> bool:
    joined = " ".join(evidence_texts)
    if any(term in joined for term in KINSHIP):
        return True
    return any(name in joined for name in index.persons)
