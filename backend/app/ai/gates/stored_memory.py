"""Bounded descriptive recollection, authorizing literal source quotes only.

Neither relevance nor a model's guess proves answerability. Require a concrete
requested category and its value with the requested predicate in one clause.
Unknown constructions deliberately remain unsupported.
"""

import re
from collections.abc import Collection, Sequence

from app.ai.gates.textrules import fold_family_terms, strip_josa

_PREDICATE = re.compile(
    r"좋아(?:하|한|해|했)|싫어(?:하|한|해|했)|선호(?:하|한|해|했)|사용(?:하|한|해|했)|보관(?:하|한|해|했)"
)
_QUESTION = re.compile(r"어떤|무슨|무엇|뭐")
_UNSAFE = re.compile(
    r"\d|작년|올해|내년|최근|최신|마지막|처음|몇\s*(?:번|개|명)|횟수|모두|전부|가장|변화|과정|왜|이유|비교|어떻게"
)
_UNCERTAIN = re.compile(
    r"아마|추측|모르|모른|거짓|농담|불확실|것\s*같|듯|할까|한다면|하면|인지|아닌지|\?"
)
_NEGATIVE = re.compile(
    r"안\s*(?:좋아|싫어|선호|사용|보관)|(?:하지|한 적이)\s*(?:않|없)|못\s*(?:좋아|선호|사용|보관)"
)


def request(query: str) -> tuple[str, str] | None:
    """Return (predicate stem, category); do not infer an omitted category."""
    if _UNSAFE.search(query) or _NEGATIVE.search(query) or not _QUESTION.search(query):
        return None
    predicates = list(_PREDICATE.finditer(query))
    if len(predicates) != 1:
        return None
    # A wh-modified category: '어떤 차를 선호해?' / '무슨 도구를 사용했어?'.
    category = re.search(
        r"(?:어떤|무슨)\s+([가-힣]+?)(?:을|를|은|는|이|가)?(?=\s|[?？]|$)", query
    )
    if not category:
        return None
    return predicates[0].group()[:2], category.group(1)


def support(
    query: str, text: str, persons: set[str], known_persons: Collection[str] = ()
) -> list[str]:
    spec = request(query)
    if spec is None:
        return []
    predicate, category = spec
    result: list[str] = []
    for sentence in re.split(r"[.!。\n]", text):
        # Different clauses cannot lend a subject/category to a predicate.
        for clause in re.split(r"하지만|그러나|그리고|;", sentence):
            folded = fold_family_terms(clause)
            if _UNCERTAIN.search(folded) or _NEGATIVE.search(folded):
                continue
            matches = [
                m
                for m in _PREDICATE.finditer(folded)
                if m.group().startswith(predicate)
            ]
            if len(matches) != 1:
                continue
            before = folded[: matches[0].start()]
            if persons:
                # A reported speaker is not necessarily the preference subject.
                if len(persons) != 1:
                    continue
                person = next(iter(persons))
                subject = re.match(
                    re.escape(person) + r"(?:은|는|이|가|께서)\s+", before.strip()
                )
                if subject is None:
                    continue
                remainder = before.strip()[subject.end() :]
                if re.search(r"[가-힣]+(?:는|이|가|께서)\s+", remainder) or any(
                    re.search(re.escape(p) + r"(?:은|는|이|가|께서)\s+", remainder)
                    for p in known_persons
                    if p not in persons
                ):
                    continue
            # A specific modifier before the category is a literal answer value.
            # '케이크를 좋아한다' alone does not answer '어떤 케이크'.
            value = re.search(
                r"([가-힣]+(?:\s+[가-힣]+)*)\s+"
                + re.escape(category)
                + r"(?:을|를)(?:\s|$)",
                before,
            )
            if not value:
                continue
            words = [strip_josa(w) for w in value.group(1).split()]
            words = [
                w
                for w in words
                if w not in persons
                and w
                not in {
                    "나",
                    "저",
                    "우리",
                    "어떤",
                    "무슨",
                    "어제",
                    "오늘",
                    "내일",
                    "항상",
                    "자주",
                    "정말",
                    "아주",
                    "매우",
                    "다시",
                }
            ]
            if not words:
                continue
            # No question, conditional or bare predicate: explicit assertion only.
            tail = folded[matches[0].start() :].strip()
            if not re.fullmatch(
                r"(?:좋아|싫어|선호|사용|보관)(?:한다|했다|합니다|했습니다|해요|했어요|함|했음|"
                r"(?:한다고|했다고)(?:\s+[가-힣]+)*\s+말했다)",
                tail,
            ):
                continue
            result.append(clause.strip())
    return result


def opposing(
    query: str, text: str, persons: set[str], positive_texts: Sequence[str]
) -> bool:
    spec = request(query)
    if spec is None:
        return False
    predicate, category = spec
    value_pattern = re.compile(r"([가-힣]+)\s+" + re.escape(category) + r"(?:을|를)")
    positive_values = {
        m.group(1)
        for t in positive_texts
        for clause in support(query, t, persons)
        for m in value_pattern.finditer(clause)
    }
    for sentence in re.split(r"[.!。\n]", fold_family_terms(text)):
        # Reuse the same subject/claim checks after removing only explicit
        # negative morphology; this transformed text is NEVER rendered/cited.
        affirmative = re.sub(r"하지\s*않는다", "한다", sentence)
        affirmative = re.sub(r"하지\s*않는다고", "한다고", affirmative)
        affirmative = re.sub(r"하지\s*않았다고", "했다고", affirmative)
        affirmative = re.sub(r"(?<![가-힣])(?:안|못)\s*(?=좋아|싫어|선호|사용|보관)", "", affirmative)
        if not support(query, affirmative, persons):
            continue
        if (
            category in sentence
            and predicate in sentence
            and _NEGATIVE.search(sentence)
            and not _UNCERTAIN.search(sentence)
            and all(p in sentence for p in persons)
            and positive_values & {m.group(1) for m in value_pattern.finditer(sentence)}
        ):
            return True
    return False


def subject(query: str) -> set[str]:
    match = re.match(r"([가-힣]+?)(?:은|는|이|가)\s+", fold_family_terms(query))
    return {match.group(1)} if match else set()
