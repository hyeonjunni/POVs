import re
from typing import Final

_VALUES: Final = {
    "한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}
_END: Final = r"(?=\s|[.,!?]|$|(?:이었|였|이다|이고|이더|이야|이네|입니다|은|는|이|가|을|를|만|도)(?:\s|[가-힣.,!?]|$))"
_COUNT: Final = re.compile(
    r"(?<![가-힣\w])(?P<number>\d+|하나|둘|셋|넷|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)"
    r"\s*(?P<counter>사람|명|개)" + _END
    + r"|(?<![가-힣\w])(?P<standalone>하나|둘|셋|넷|다섯|여섯|일곱|여덟|아홉)" + _END
)


def count_atoms(text: str) -> list[tuple[str, int, str]]:
    atoms: list[tuple[str, int, str]] = []
    for match in _COUNT.finditer(text):
        number = match.group("number") or match.group("standalone")
        value = int(number) if number.isdigit() else _VALUES[number]
        counter = match.group("counter") or ""
        atoms.append((match.group(), value, "명" if counter == "사람" else counter))
    return atoms


def has_requested_count(query: str, text: str) -> bool:
    counter = re.search(r"몇\s*(사람|명|개)", query)
    expected = counter.group(1) if counter else ""
    expected = "명" if expected == "사람" else expected
    locations = re.findall(r"(?<![가-힣])([가-힣]{2,}?)(?:에|에서)(?=\s)", query)
    return all(place in text for place in locations) and any(
        not expected or not unit or expected == unit for _, _, unit in count_atoms(text))
