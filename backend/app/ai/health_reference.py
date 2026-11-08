import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class HealthReference:
    condition: str
    source_url: str
    summary: str
    checked_on: str = "2026-10-15"


REFERENCES: Final = (
    HealthReference(
        "빈혈", "https://www.nhs.uk/conditions/iron-deficiency-anaemia/",
        "빈혈은 원인이 다양하므로 빈혈이라는 말만으로 철결핍이라고 확정할 수 없다. "
        "철결핍성 빈혈의 식사 관리에는 고기, 콩·렌틸콩, 짙은 녹색 잎채소, 철분 강화 시리얼, "
        "건포도 같은 말린 과일 등 철분이 있는 식품이 도움이 될 수 있다. "
        "차와 커피를 많이 마시면 철분 흡수를 방해할 수 있다. "
        "음식만으로 모든 빈혈을 치료할 수는 없으며, 철분제 복용은 원인을 확인한 뒤 의료진과 상의한다.",
    ),
    HealthReference(
        "저혈압", "https://www.nhs.uk/conditions/low-blood-pressure-hypotension/",
        "저혈압의 일반 생활 관리에는 충분한 수분 섭취, 소량씩 자주 식사하기, "
        "앉거나 누운 자세에서 천천히 일어나기, 과음 피하기가 있다. "
        "특정 음식이 저혈압을 치료한다고 단정하지 않는다. "
        "어지럼이나 실신이 반복되면 진료를 받는다. 소금이나 카페인 증량, 약 변경을 임의로 권하지 않는다.",
    ),
    HealthReference(
        "고혈압", "https://www.nhs.uk/conditions/high-blood-pressure/",
        "고혈압의 일반 생활 관리에는 균형 잡힌 식사, 짠 음식과 식사에 추가하는 소금 줄이기, "
        "규칙적인 운동, 과음과 과도한 카페인 피하기가 있다. "
        "개인의 검사 수치만으로 이 정보에서 새 진단을 내리거나 약의 종류·용량을 정하지 않는다.",
    ),
)

_ADVICE: Final = re.compile(
    r"음식|식단|먹(?:으면|어야|을|기|는\s*게)|식사.*(?:좋|추천|관리)|"
    r"관리|생활\s*습관|예방|도움|어떻게\s*(?:하|해)|조심|주의|피해야|피하는\s*게"
)
_PAST_MEAL: Final = re.compile(r"먹었|먹던|먹은|식사했")


def select_health_references(query: str, context: str = "") -> tuple[HealthReference, ...]:
    """Select advice sources; context must be relevant, authorized conversation evidence.

    Explicit conditions override inherited conditions. Returning no sources is not
    an answer gate: the caller continues its ordinary record-answering path.
    """
    if not _ADVICE.search(query) or _PAST_MEAL.search(query):
        return ()
    explicit = tuple(ref for ref in REFERENCES if ref.condition in query)
    if explicit:
        return explicit
    return tuple(ref for ref in REFERENCES if ref.condition in context)
