import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class CountRecord:
    id: str
    type: str
    title: str
    category: str
    kind: str
    event_ids: tuple[str, ...]
    date: str
    authored: str
    owner: str
    subjects: tuple[str, ...]
    participants: tuple[str, ...]
    visibility: str = ""
    authors: tuple[str, ...] = ()  # approved PERSPECTIVE members: who wrote it (never assumed from the uploader)


@dataclass(frozen=True, slots=True)
class RecordCount:
    answer: str
    record_ids: tuple[str, ...]


_MEDIA_TERMS: Final = r"사진|이미지|결과표|음성|녹음|오디오|영상|비디오|텍스트"
_ACCESS_CLAUSE: Final = (
    r"(?<![가-힣])(?:내가|제가|나는|저는|나|저|내\s*계정(?:에서)?|현재\s*계정(?:에서)?)\s*"
    r"(?:(?:볼|열람할|확인할|접근할)\s*수\s*있는|보이는|열람\s*가능한)"
    r"|(?<![가-힣])(?:나|저)(?:에게|한테)\s*보이는"
)


def normalize_count_query(query: str, previous_query: str = "") -> str:
    """Separate access scope from people, and replace prior aggregation dimensions."""
    access_scope = bool(re.search(_ACCESS_CLAUSE, query))
    query = re.sub(_ACCESS_CLAUSE, "", query).strip()
    if access_scope or not previous_query or not re.match(r"^(?:그럼|그러면|그중|그 중|거기|아까|이전|당시)", query):
        return query
    previous = re.sub(_ACCESS_CLAUSE, "", previous_query).strip()
    domains = (r"일기", r"건강검진|검진|검사", r"여행", r"생일", r"사건")
    current_domains = {domain for domain in domains if re.search(domain, query)}
    prior_domains = {domain for domain in domains if re.search(domain, previous)}
    if current_domains and prior_domains and not current_domains & prior_domains:
        return query
    if re.search(_MEDIA_TERMS, query):
        previous = re.sub(_MEDIA_TERMS, "", previous)
    if re.search(r"(?:19|20)\d{2}년|올해|작년|재작년", query):
        previous = re.sub(r"(?:19|20)\d{2}년|재작년|작년|올해", "", previous)
    if re.search(r"개인|비공개|공유|공동", query):
        previous = re.sub(r"개인|비공개|공유|공동", "", previous)
    if "전체" in query:
        return query
    return " ".join((previous + " " + query).split())


def is_diary(record_type: str, kind: str, title: str) -> bool:
    """One diary definition for counting and listing: a written, non-event record titled 일기."""
    return record_type == "text" and kind != "EVENT" and "일기" in title


def is_count_query(query: str) -> bool:
    return bool(re.search(r"몇\s*(?:개|건|장|번|회|차례)|개수|갯수|횟수|건수", query)
                and re.search(r"기록|사진|이미지|음성|녹음|오디오|영상|비디오|텍스트|일기|여행|검진|검사|생일|사건", query))


def count_records(query: str, records: list[CountRecord], people: set[str]) -> RecordCount:
    from app.ai.event_memory import matches_topic
    types = []
    for pattern, label, kind, unit in (
        (r"사진|이미지|결과표", "사진", "image", "장"),
        (r"음성|녹음|오디오", "음성", "voice", "개"),
        (r"영상|비디오", "영상", "video", "개"),
        (r"텍스트", "텍스트", "text", "개"),
    ):
        if re.search(pattern, query):
            types.append((label, kind, unit))
    diary = "일기" in query
    health = bool(re.search(r"검진|검사", query))
    trip = "여행" in query
    birthday = "생일" in query
    record_units = bool(re.search(r"기록.*(?:몇\s*(?:개|건)|개수|갯수|건수)", query))
    event = (health or trip or birthday) and not types and not diary and not record_units
    if not types:
        types = [("일기" if diary else "건강검진" if health else "여행" if trip else "생일" if birthday else "사건 기록" if "사건" in query else "기록",
                  "", "회" if event else "개")]
    years = set(re.findall(r"(?:19|20)\d{2}(?=년)", query))
    current_year = datetime.now(ZoneInfo("Asia/Seoul")).year
    for term, offset in (("올해", 0), ("작년", -1), ("재작년", -2)):
        if term in query and not (term == "작년" and "재작년" in query):
            years.add(str(current_year + offset))
    written = diary or bool(re.search(r"쓴|작성|올린|업로드", query))
    places = set(re.findall(r"속초|오사카|오키나와|타이베이|제주|전주|강릉|가평|용인|서울숲", query.replace("대만", "타이베이")))
    candidates = []
    for record in records:
        if re.search(r"개인|비공개", query) and record.visibility != "PRIVATE":
            continue
        if re.search(r"공유|공동", query) and record.visibility not in ("FAMILY", "CUSTOM"):
            continue
        if diary and not is_diary(record.type, record.kind, record.title):
            continue
        if "사건" in query and record.kind != "EVENT":
            continue
        if health and record.category not in ("HEALTH_EXAM", "HEALTH_REPORT"):
            continue
        # One travel definition with the travel tool: trips plus outings (event_memory.matches_topic).
        if trip and not matches_topic(record.category, record.title, "", "여행") or birthday and record.category != "BIRTHDAY":
            continue
        if "해외" in query and not re.search(r"오사카|오키나와|타이베이", record.title):
            continue
        if places and not any(place in record.title for place in places):
            continue
        related = set(record.subjects) if health or birthday else set(record.subjects + record.participants)
        # '쓴/작성한' is authorship; '올린/업로드' is the uploader.
        writers = {record.owner} if re.search(r"올린|업로드", query) else set(record.authors)
        if people and not (bool(writers & people) if written else people <= related):
            continue
        date = record.authored or record.date if diary else record.date
        if years and not any(year in date for year in years):
            continue
        candidates.append(record)
    lines, matched, examination_dates = [], set(), []
    for label, kind, unit in types:
        rows = [r for r in candidates if not kind or r.type == kind or kind == "voice" and r.type == "audio"]
        if event:
            groups = {}
            for record in rows:
                dates = re.findall(r"\d{4}-\d{2}-\d{2}", record.date)
                if health:
                    if not dates:
                        continue
                    keys = [(tuple(sorted(record.subjects or (record.owner,))), dates[0])]
                else:
                    keys = record.event_ids or ((record.date, record.title),)
                for key in keys:
                    groups.setdefault(key, record)
            rows = list(groups.values())
        matched.update(r.id for r in rows)
        if trip and event:
            outings = sum(r.category != "TRIP" for r in rows)
            lines.append(f"여행·나들이 {len(rows)}회(여행 {len(rows) - outings}회, 나들이 {outings}회)")
        else:
            lines.append(label + " " + str(len(rows)) + unit)
        if health and event and rows:
            examination_dates = sorted({re.findall(r"\d{4}-\d{2}-\d{2}", r.date)[0] for r in rows})
    scope = "현재 계정에서 확인할 수 있는 기록 기준으로,"
    if years:
        scope += " " + ", ".join(sorted(years)) + "년의"
    answer = scope + " " + ", ".join(lines) + "입니다."
    if examination_dates:
        answer += " 검진일은 " + ", ".join(examination_dates) + "입니다."
    return RecordCount(answer, tuple(sorted(matched)))
