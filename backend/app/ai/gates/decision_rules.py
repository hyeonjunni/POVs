"""Bounded Korean commitment recognition; never rewrite evidence or resolve dates.

These rules authorize literal quotations, not a generated yes/no affirmation.
Unknown morphology/semantic contradictions remain outside this deterministic gate.
"""
from __future__ import annotations

import re

from app.ai.gates.textrules import fold_family_terms, normalize_query

_COMMITMENT = re.compile(
    r"(?:기로(?:(?:결정|합의|약속)(?:을)?)?|(?:결정|합의|약속|계획)(?:을|를)?)"
    r"(?:했|하였|한(?=게|것|거|건|걸|일|내용|약속|계획|결정|합의)|함)")
_ASSERTION = re.compile(
    r"(?:기로(?:(?:결정|합의|약속)(?:을)?)?|(?:결정|합의|약속|계획)(?:을|를)?)"
    r"(?:(?:했(?:었)?|하였(?:었)?)(?:다|습니다|어요|음)|함)$")
_UNCERTAIN = re.compile(r"(?:아마|추측|짐작|모르|불확실|것같|듯|할까|하면|한다면)")
_YEAR = re.compile(
    r"(?<!\d)(\d{4})(?:\s*년|(?=\s|$)"
    r"(?!\s*(?:원|만원|명|개|번|미터|킬로|kg|km)(?:[은는이가을를에]|\b)))")
_PARTICLES = re.compile(r"(?:부터는|까지는|에서는|으로는|에서|부터|까지|으로|관련해서|관련해|은|는|을|를|에|이|가)$")
_FILLERS = re.compile(
    r"어떻게|무엇을?|뭐라고|뭘|뭐였지|뭐|무슨|어떤|앞으로|관련해서|관련해|"
    r"직접|하기로|하였었지|했었지|했지|한게|한것|결정|합의|약속|계획")
_CONTEXT = frozenset({"작년", "올해", "내년", "다음", "지난", "이번", "가족", "가족들", "기록", "우리", "안", "하", "하기", "않", "하지"})


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", normalize_query(fold_family_terms(text)))


def requests_decision(query: str) -> bool:
    """Completed/nominal commitment, including polar questions; not '뭐할까'."""
    compact = _compact(query)
    if re.search(r"(?:했|하였)(?:었)?(?:으면|다면|더라면)|한다면", compact):
        return False
    return bool(_COMMITMENT.search(compact))


def years_compatible(left: str, right: str) -> bool:
    """Only explicit years can contradict; relative words have no calendar anchor."""
    a, b = set(_YEAR.findall(left)), set(_YEAR.findall(right))
    return not (a and b and not a & b)


def _word(token: str) -> str:
    # Unlike generic content_tokens, retain one-syllable subjects (전, 집, 차).
    return _PARTICLES.sub("", token)


def decision_topics(query: str, context=()) -> set[str]:
    prefix = re.split(r"기로|결정|합의|약속|계획", query, maxsplit=1)[0]
    prefix = _FILLERS.sub(" ", prefix)
    if "기로" in query:
        # The governed verb is an action, not a second requested subject.
        prefix = re.sub(r"[가-힣]+\s*$", "", prefix)
    words = re.findall(r"[가-힣]+|\d+", prefix)
    excluded = _CONTEXT | set(context)
    # Object/topic particles identify the subject independently of the action.
    subjects = {_word(w) for w in words if re.search(r"[가-힣](?:을|를|은|는)$", w)} - excluded
    if subjects:
        return subjects
    return {_word(w) for w in words if not w.isdigit()} - excluded - {"", "년"}


def _requested_actions(query: str) -> set[str]:
    query = re.sub(r"어떻게|무엇을?|뭐라고|뭘|뭐|관련해서|관련해", " ", query)
    return ({verb for verb, _ in action_polarities(query) if verb != "하"}
            | {m.group(1) + "하" for m in re.finditer(r"([가-힣]+?)해서", query)})


def _mentions_action(sentence: str, verb: str) -> bool:
    # Regular 하다 contractions only; no synonym or free-form semantic inference.
    surface = re.escape(verb[:-1]) + r"\s*(?:하|해|했)" if verb.endswith("하") else re.escape(verb)
    boundary = r"(?:(?<![가-힣])|(?<=않고)|(?<=해서))"
    return bool(re.search(boundary + r"(?:안\s*|못\s*)?" + surface
                         + r"\s*(?:기로|지|고|서|기|려|면|는|다|$)", sentence))


def decision_statements(query: str, text: str, context=()) -> list[str]:
    """Require the requested subject in the asserted commitment sentence itself.

    A header/other sentence mentioning the subject cannot lend it to an unrelated
    commitment. Returned spans remain original text, including both sides of a
    negative compound action; actual answer rendering still uses the whole chunk.
    """
    topics = decision_topics(query, context)
    actions = _requested_actions(query)
    negative_selection = bool(re.search(r"(?:안하기로|않기로)한", _compact(query))
                              and re.search(r"뭐|무엇|뭘", query))
    found = []
    for match in re.finditer(r"[^.!?。\n]+[.!?。]?", text):
        sentence = match.group().strip()
        compact = _compact(sentence.rstrip(".!。"))
        if not _ASSERTION.search(compact) or _UNCERTAIN.search(compact):
            continue
        if not years_compatible(query, sentence):
            continue
        if negative_selection and not any(not polarity for _, polarity in action_polarities(sentence)):
            continue
        # An earlier completed activity must not lend its subject to the actual
        # commitment clause. Keep '하지 않고 ...' together: it is a compound plan.
        clause = re.split(r"(?:했|었|았)(?:고|지만|는데)|그리고|하지만|그러나|;", sentence)[-1]
        tokens = re.findall(r"[가-힣]+", fold_family_terms(clause))
        words = {_word(w) for w in tokens}
        objects = [_word(w) for w in tokens if re.search(r"[가-힣](?:을|를)$", w)
                   and _word(w) not in set(context) | {"결정", "합의", "약속", "계획"}]
        if "기로" in compact and topics and objects and objects[-1] not in topics:
            continue
        if "기로" not in compact and not re.search(r"(?:을|를|으로|라고)\s*(?:결정|합의|약속|계획)", clause):
            # '결정했다' alone reports existence, not the content of a decision.
            continue
        if "기로" not in compact and topics:
            nominal = re.split(r"결정|합의|약속|계획", clause)[0]
            content = {_word(w) for w in re.findall(r"[가-힣]+", nominal)}
            if not content - topics - set(context) - _CONTEXT:
                continue
        if topics <= words and all(_mentions_action(clause, verb) for verb in actions):
            found.append(sentence)
    return found


def action_polarities(statement: str) -> set[tuple[str, bool]]:
    """Recognize explicit opposite actions, including '하지 않고 ... 하기로'.

    Do not label a compound commitment globally negative: each verb owns its
    polarity. No equivalence between different verbs is invented here.
    """
    actions = set()
    statement = statement.replace("안하기로", "안 하기로")
    for match in re.finditer(r"([가-힣]+?)\s*지\s*않(?:기|고)", statement):
        actions.add((match.group(1), False))
    for match in re.finditer(r"(?:(?<![가-힣])(안|못)\s+)?([가-힣]+?)\s*기로", statement):
        if match.group(2) != "않" and not match.group(2).endswith("지않"):
            actions.add((match.group(2), not bool(match.group(1))))
    return actions


def opposite_commitments(left: str, right: str) -> bool:
    a, b = action_polarities(left), action_polarities(right)
    return any((verb, not polarity) in b for verb, polarity in a)
