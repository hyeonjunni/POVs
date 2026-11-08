import re
from typing import Final

from app.ai.gates.entity_index import PLACE_SUFFIXES
from app.ai.gates.textrules import content_tokens

_REQUEST: Final = re.compile(
    r"(?<![가-힣])([가-힣]+?)(?:은|는|을|를)\s+([^?]*?)어디에\s+(가져다\s*놓|놓|보관)"
)
_UNCERTAIN: Final = re.compile(r"모르|모른|아마|추측|않|못|안\s|인지|는지|다면|할까|것\s*같|\?")


def location_request(query: str) -> tuple[str, str, frozenset[str]] | None:
    match = _REQUEST.search(query)
    return (match.group(1), match.group(3).replace(" ", ""),
            frozenset(content_tokens(match.group(2)))) if match else None


def location_supported(query: str, text: str) -> bool:
    request = location_request(query)
    if request is None or re.search(r"지금|현재|정확|주소|선반|최근|최신", query):
        return False
    subject, predicate, context = request
    for sentence in re.split(r"[.!。\n]", text):
        if _UNCERTAIN.search(sentence):
            continue
        if not re.search(r"(?<![가-힣])" + re.escape(subject) + r"(?:은|는|을|를)\s", sentence):
            continue
        if predicate not in sentence.replace(" ", ""):
            continue
        if not context <= content_tokens(sentence):
            continue
        places = re.findall(r"(?<![가-힣])([가-힣]+?)(?:에|으로|로)\s", sentence)
        relative_place = re.search(
            r"(?:[가-힣]+(?:온|던|한|는)|원래|같은)\s+(?:곳|자리)(?:에|로|으로)\s", sentence)
        if any(place.endswith(PLACE_SUFFIXES) for place in places) or relative_place:
            return True
    return False
