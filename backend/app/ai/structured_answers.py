"""Deterministic answers that must not be produced from top-k retrieval or free generation.

Every function receives only the viewer's authorized corpus (already ACL-filtered by
search_secure_room) and returns None when the question is not its own.
"""
import re
from dataclasses import replace

from app.ai.grounded_answering import GroundedAnswerSnapshot, build_answer_plan, build_fact_sheet, build_query_frame
from app.ai.query_normalization import is_speech_act
from app.ai.record_counts import is_count_query, is_diary

DIARY_PAGE_SIZE = 10

_OWN_DIARY = r"(?:내가|제가|내|나|저).*일기"
_LIST_CUE = r"목록|리스트|전부|모두|전체|(?<![가-힣])다(?=\s|$)|뭐\s*있|무슨\s*일기|어떤\s*일기"
# A plain own-diary request names only the author, the diary and how to ask ('내 일기장 보여줘').
_PLAIN_DIARY_WORD = (r"(?:내가|제가|내|나|저)?(?:쓴|작성한|적은|적어둔|써둔|써놓은)|내가|제가|내|나|저|직접|어떤|무슨|뭐|머|목록"
                     r"|일기(?:장)?(?:들)?(?:을|를|은|는|이|가|도)?")
_MORE = (r"\s*(?:더|다음|계속|나머지)(?:\s*(?:거|것|페이지|목록|일기))?(?:도|를|을)?\s*"
         r"(?:보여|알려)?(?:줘|주세요|줄래)?[?？.!\s]*")
_REFERENT = r"(?<![가-힣])(?:걔|쟤|얘|그\s?애)(?:가|는|를|랑|이랑|의|한테|도|이)?(?![가-힣])"
_OVERVIEW = r"(?:모든|전체|전부|모두)\s*(?:가족\s*)?기록.*(?:정리|요약)|기록.*(?:전부|모두|다)\s*(?:정리|요약)"
_FULL_DATE = r"((?:19|20)\d{2})\s*(?:년\s*|[-./])(\d{1,2})\s*(?:월\s*|[-./])(\d{1,2})\s*일?"


def _snapshot(query, payload, route, answer, corpus, chunks=()):
    sheet = build_fact_sheet(list(chunks))
    plan = build_answer_plan(sheet, k=len(sheet.facts)) if sheet.facts else None
    return GroundedAnswerSnapshot(replace(build_query_frame(query), raw_query=payload.query), payload.model_id,
                                  route, answer, build_fact_sheet(corpus.chunks), plan, corpus, record_discovery=True)


def ambiguous_referent(query, payload, corpus, personal, bindings):
    """'걔 생일 언제야?' names nobody; guessing a family member is worse than asking."""
    if not re.search(_REFERENT, payload.query) or bindings:
        return None
    if personal and any(p["name"] in payload.query for p in personal["people"]):
        return None
    return _snapshot(query, payload, "CLARIFY", "누구를 말씀하시는지 확인하지 못했어요. "
                     "이름이나 호칭(예: 엄마, 오빠)으로 다시 물어봐 주세요.", corpus)


def archive_overview(query, payload, metadata, corpus):
    """A whole-archive summary is answered with counts, not by pasting every original into the model.

    It is a CLARIFY (narrow the question) carrying the counts: no single record supports it,
    and the ask contract requires every ANSWER to cite at least one record."""
    if not re.search(_OVERVIEW, payload.query):
        return None
    labels = {"text": "글", "image": "사진", "audio": "음성", "voice": "음성", "video": "영상"}
    kinds = {}
    for source in corpus.records:
        row = source[1]
        label = labels.get(row.get("type", "text"), "기타")
        kinds[label] = kinds.get(label, 0) + 1
    dates = sorted(d for rid in metadata for d in re.findall(
        r"\d{4}-\d{2}-\d{2}", (metadata[rid].get("m2_metadata") or {}).get("occurred_at") or ""))
    events = sum((metadata[rid].get("m2_metadata") or {}).get("record_kind") == "EVENT" for rid in metadata)
    answer = (f"현재 계정으로 볼 수 있는 기록은 총 {len(corpus.records)}건입니다("
              + ", ".join(f"{label} {n}건" for label, n in kinds.items()) + ").")
    if events:
        answer += f" 이 중 사건 기록은 {events}건입니다."
    if dates:
        answer += f" 기록된 날짜는 {dates[0]}부터 {dates[-1]}까지입니다."
    answer += (" 모든 원문을 한 번에 요약하면 내용이 잘리거나 부정확해질 수 있어 전체 요약은 하지 않았습니다. "
               "기간·사람·주제를 정해 물어봐 주세요(예: '2025년 여행 정리해줘', '엄마랑 무슨 일 있었어?').")
    return _snapshot(query, payload, "CLARIFY", answer, corpus)


def _plain_diary_words(words):
    return all(is_speech_act(word) or re.fullmatch(_PLAIN_DIARY_WORD, word) for word in words)


def _named_author(text, personal, plain_only=True):
    """(user_id, role, label) when the question asks only for one family member's diaries:
    '아빠가 쓴 일기' is authorship (PERSPECTIVE); '엄마에 대한 일기' is the subject (SUBJECT), never an author filter."""
    if not personal:
        return None
    from app.ai.record_questions import resolve_addresses
    resolved, _ = resolve_addresses(text, personal)
    found = []
    for person in personal["people"]:
        forms = [person["name"]] + ([person["name"][1:]] if len(person["name"]) == 3 else [])
        for form in forms:
            about = re.search(re.escape(form) + r"(?:에\s*(?:대한|관한)|\s*관련(?:된)?)\s*일기(?:장)?(?:들)?(?:을|를|은|는|이|가|도)?", resolved)
            match = about or re.search(re.escape(form) + r"(?:이|가|의)?\s*(?:(?:쓴|작성한|적은|남긴|써둔|써놓은)\s*)?일기(?:장)?(?:들)?(?:을|를|은|는|이|가|도)?",
                                       resolved)
            if match:
                role, label = ("SUBJECT", person["name"] + "님에 대한") if about else ("PERSPECTIVE", person["name"] + "님이 작성한")
                found.append(((person["user_id"], role, label), resolved[:match.start()] + " " + resolved[match.end():]))
                break
    if len(found) != 1 or personal["viewer_name"] in resolved:
        return None
    scope, rest = found[0]
    return scope if not plain_only or _plain_diary_words(re.findall(r"[\w가-힣]+", rest)) else None


# Relation words that name a person; in an author phrase, one left unresolved means the author is unknown.
_KIN = (r"엄마|아빠|어머니|아버지|어머님|아버님|오빠|오라버니|형|누나|언니|동생|여동생|남동생|아들|딸|여보|남편|아내|와이프"
        r"|할머니|할아버지|외할머니|외할아버지|손자|손녀|이모|삼촌|고모|큰아빠|작은아빠")
_AUTHOR_VERB = r"(?:쓴|작성한|적은|남긴|써둔|써놓은)\s*일기"


def ambiguous_diary_author(text, personal):
    """True when the question names a diary author ('…가 쓴 일기') that is not exactly one resolved member:
    two people, or a relation word bound to nobody. The answer must ask, never pick one or drop the author."""
    match = re.search(_AUTHOR_VERB, text)
    if not personal or not match:
        return False
    from app.ai.record_questions import resolve_addresses
    phrase, _ = resolve_addresses(text[:match.start()], personal)
    members = {p["name"] for p in personal["people"] if p["name"] in phrase}
    if personal["viewer_name"] in phrase:
        members.add(personal["viewer_name"])
    unresolved = re.search(rf"(?<![가-힣])(?:{_KIN})(?=(?:이랑|랑|하고|와|과|이|가|의|도|에)?(?:\s|$))", phrase)
    return len(members) > 1 or bool(unresolved)


def diary_scope(text, user_id, personal):
    """(member, role, label) for any explicitly scoped diary question, with or without a topic
    ('아빠가 쓴 일기 중 여행 얘기'): retrieval must stay inside that scope."""
    named = _named_author(text, personal, plain_only=False)
    if named:
        return named
    return (user_id, "PERSPECTIVE", "본인이 작성한") if re.search(_OWN_DIARY, text) else None


def _diary_scope(text, user_id, personal):
    if is_count_query(text):
        return None
    # A named member wins: '누나 일기' contains '나' but asks for the sister's diaries.
    named = _named_author(text, personal)
    if named:
        return named
    if re.search(_OWN_DIARY, text) and (re.search(_LIST_CUE, text) or _plain_diary_words(re.findall(r"[\w가-힣]+", text))):
        return user_id, "PERSPECTIVE", "본인이 작성한"
    return None


def own_diary_page(query, payload, user_id, metadata, corpus, page_size=DIARY_PAGE_SIZE, personal=None, roles=None):
    """Complete diary list with a stateless cursor ('더 보여줘' continues after the last cited ID).

    Scope is the viewer's own diaries, one named member's diaries (author = approved PERSPECTIVE member) or the
    diaries about one member (approved SUBJECT). The uploader is never assumed to be the author, and a diary
    with no recorded author is nobody's. The corpus is already ACL-filtered, so another member's list holds only
    diaries shared with the viewer, and an empty list reads the same whether or not private diaries exist.
    """
    roles = roles or {}
    text = payload.query
    scope = _diary_scope(text, user_id, personal)
    more = bool(re.fullmatch(_MORE, text) and payload.history and payload.context_mode != "ROOM"
                and payload.selected_record_id is None)
    if not scope and not more:
        return None
    listing = bool(scope)
    if not listing:
        # Continue the scope of the list being paged: skip only a contiguous run of '더 보여줘' turns, so a
        # list from before a topic change is never revived.
        previous = next((turn.query for turn in reversed(payload.history) if not re.fullmatch(_MORE, turn.query)), "")
        scope = _diary_scope(previous, user_id, personal)
        if not scope:
            return None
        shown = [str(rid) for rid in payload.history[-1].record_ids]
    member, role, label = scope
    diaries = []
    for source in corpus.records:
        row = source[1]
        fields = metadata[row["id"]].get("m2_metadata") or {}
        if member in roles.get(row["id"], {}).get(role, ()) and is_diary(
                row.get("type", "text"), fields.get("record_kind", ""), metadata[row["id"]].get("title", "")):
            diaries.append((fields.get("authored_at") or fields.get("occurred_at") or "", row["id"]))
    # Newest first; record id breaks ties so every page boundary is stable.
    ids = [rid for _, rid in sorted(diaries, key=lambda item: (item[0], item[1]), reverse=True)]
    start = 0
    if not listing:
        if not shown or not set(shown) <= set(ids):
            return None
        start = max(ids.index(rid) for rid in shown) + 1
    if not ids:
        return _snapshot(query, payload, "NO_RECORD", f"현재 계정에서 {label} 일기를 찾지 못했습니다.", corpus)
    page = ids[start:start + page_size]
    if not page:
        return _snapshot(query, payload, "NO_RECORD", f"{label} 일기 {len(ids)}개를 모두 보여드렸습니다.", corpus)
    first = {}
    for chunk in corpus.chunks:
        if chunk.record_id in page and (chunk.record_id not in first or chunk.ordinal < first[chunk.record_id].ordinal):
            first[chunk.record_id] = chunk
    lines = []
    for number, rid in enumerate(page, start + 1):
        fields = metadata[rid].get("m2_metadata") or {}
        when = (fields.get("authored_at") or fields.get("occurred_at") or "날짜 미상")[:10]
        lines.append(f"{number}. {when} · {metadata[rid].get('title', '일기')}")
    end = start + len(page)
    prefix = "" if (member, role) == (user_id, "PERSPECTIVE") else "현재 계정으로 볼 수 있는 기록 중 "
    answer = (f"{prefix}{label} 일기는 총 {len(ids)}개입니다. 최신순 {start + 1}–{end}번째입니다.\n" + "\n".join(lines))
    answer += (f"\n남은 일기 {len(ids) - end}개는 '더 보여줘'라고 입력하면 이어서 보여드립니다." if end < len(ids)
               else "\n마지막 페이지입니다.")
    return _snapshot(query, payload, "ANSWER", answer, corpus, [first[rid] for rid in page if rid in first])


def explicit_dates(query):
    return [f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in re.findall(_FULL_DATE, query)]


def on_date(occurred_at, day):
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", occurred_at or "")
    return bool(dates) and dates[0] <= day <= dates[-1]


def records_on_dates(days, metadata):
    return {rid for rid, record in metadata.items()
            if any(on_date((record.get("m2_metadata") or {}).get("occurred_at"), day) for day in days)}


def no_record_on_dates(query, payload, days, corpus):
    return _snapshot(query, payload, "NO_RECORD", ", ".join(days) + "에 해당하는 기록을 현재 계정에서 찾지 못했습니다. "
                     "날짜가 다르게 기록되었을 수 있으니 사건 이름이나 장소로도 물어봐 주세요.", corpus)
