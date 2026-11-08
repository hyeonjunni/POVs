import re
from typing import Final

from app.ai.gates.textrules import content_tokens, fold_family_terms

_QUESTION: Final = re.compile(r"(?:무엇|뭐)(?:을|를)\s+")
_ACTION: Final = re.compile(r"(?<![가-힣])(?:적었|썼|먹었|읽었|샀|받았)(?:어|어요|다|습니다)[?？]?$")
_UNCERTAIN: Final = re.compile(r"모르|모른|아마|추측|않|못|안\s|인지|는지|다면|할까|것\s*같|\?")
_OBJECT: Final = re.compile(r"([가-힣]+)(?:을|를|만)\s+")


def object_request(query: str) -> tuple[str, frozenset[str]] | None:
    question = _QUESTION.search(query)
    action = _ACTION.search(query.strip())
    if not question or not action:
        return None
    context = content_tokens(fold_family_terms(query[:question.start()] + query[question.end():action.start()]))
    if not context:
        return None
    return action.group()[:2], frozenset(context)


def object_supported(query: str, text: str) -> bool:
    request = object_request(query)
    if request is None:
        return False
    action, context = request
    for sentence in re.split(r"[.!。\n,;]|(?<=고)\s+|하지만|그러나|그리고", text):
        if _UNCERTAIN.search(sentence):
            continue
        match = _ACTION.search(sentence.strip())
        if not match or not match.group().startswith(action):
            continue
        before = sentence.strip()[:match.start()]
        if _OBJECT.search(before) and context <= content_tokens(fold_family_terms(before)):
            return True
    return False
