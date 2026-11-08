import logging
import re

from app.schemas.structured_query import StructuredFilters
from app.services import secure_family_context

logger = logging.getLogger(__name__)


def categories(query):
    rules = (
        (r"여행|속초|오사카|제주|타이베이|오키나와|캠핑", ["TRIP"]),
        (r"검진|검사|결과표|혈압|혈색소|헤모글로빈|페리틴|빈혈|진단|진료", ["HEALTH_EXAM", "HEALTH_REPORT", "DIAGNOSIS"]),
        (r"생일", ["BIRTHDAY"]),
        (r"입대|전역|군대|복무|휴가", ["MILITARY"]),
        (r"입학|복학|수능|학교|축제|중간고사|모의시험|진로|학과|퀴즈|과제", ["EDUCATION"]),
        (r"선물|셔츠|가방|필통|이어폰|파우치|담요", ["BIRTHDAY"]),
        (r"도자기|인형", ["OTHER"]),
    )
    return list(dict.fromkeys(kind for pattern, kinds in rules if re.search(pattern, query) for kind in kinds))


def matches_topic(category: str, title: str, location: str, query: str) -> bool:
    kinds = categories(query)
    return category in kinds or (
        "TRIP" in kinds and category == "OTHER"
        and bool(re.search(r"나들이|산책|공원|수족관|해변|숙소|소극장|공방", title + " " + location))
    )


def from_records(metadata, query):
    memories = []
    for rid, record in metadata.items():
        fields = record.get("m2_metadata", {})
        if not matches_topic(fields.get("category", ""), record.get("title", ""),
                             fields.get("location") or "", query) or not fields.get("occurred_at"):
            continue
        raw_date = fields["occurred_at"]
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", raw_date)
        memories.append({"event_id": (fields.get("event_ids") or [rid])[0], "title": record.get("title", ""),
            "date": dates[0] if dates else raw_date, "end": dates[1] if len(dates) > 1 else None,
            "precision": "RANGE" if len(dates) > 1 else "EXACT" if dates else "APPROXIMATE",
            "authored_at": fields.get("authored_at"), "category": fields["category"],
            "record_kind": fields.get("record_kind"),
            "location": fields.get("location"), "participants": [], "record_ids": [rid]})
    return memories


def retrieve(runtime, user_id, room_id, query, allowed_ids, *, include_all=False):
    kinds = categories(query)
    if runtime is None or not kinds and not include_all:
        return []
    if "TRIP" in kinds and "OTHER" not in kinds:
        kinds.append("OTHER")
    try:
        result = secure_family_context.query(runtime, user_id, room_id, "structured_ask_query", query,
            filters=StructuredFilters(operation="FILTER", event_types=[] if include_all else kinds,
                include_participants=True, limit=500 if include_all else 100))
    except Exception as error:
        logger.warning("Optional event memory unavailable", extra={"failure_type": type(error).__name__})
        return []
    if result is None or result.route != "ANSWER":
        return []
    memories = []
    for entry in result.entries:
        if not include_all and not matches_topic(entry.event_type, entry.title, entry.location.get("label") or "", query):
            continue
        sources = list(dict.fromkeys(source.record_id for source in entry.sources))
        if not sources or not set(sources) <= allowed_ids:
            continue
        when = entry.occurred_at
        memories.append({"event_id": entry.event_id, "title": entry.title,
            "category": entry.event_type,
            "date": when.value or when.start, "end": when.end, "precision": when.precision,
            "location": entry.location.get("label"), "participants": [
                {"member_id": member, "name": result.member_labels.get(member, member), "modes": modes}
                for member, modes in entry.member_modes.items()], "record_ids": sources})
    return memories


def rank(memories, query, personal):
    names = {person["name"] for person in personal["people"] if person["name"] in query} if personal else set()
    latest = bool(re.search(r"마지막|최근|최신|가장 늦|제일 늦", query))
    earliest = bool(re.search(r"처음|최초|첫 번째|첫번째|첫\s*(?:건강|검진|생일)", query)) and not latest
    together = bool(re.search(r"같이|함께|단둘|둘이", query))
    def score(memory):
        participants = {p["name"] for p in memory["participants"] if "ONSITE" in p["modes"]}
        entity = len(names & participants)
        subject = sum(name in memory["title"] or len(name) == 3 and name[1:] in memory["title"] for name in names)
        if together and personal:
            entity += personal["viewer_name"] in participants
        terms = [term for term in re.findall(r"[가-힣]{2,}", query) if term in memory["title"]]
        pair = bool(re.search(r"단둘|둘이", query) and len(participants) == 2 and names <= participants
                    and personal and personal["viewer_name"] in participants)
        date = memory["end"] or memory["date"] or ""
        temporal = -int(re.sub(r"\D", "", date)[:8].ljust(8, "0")) if earliest else date
        objects = ("셔츠", "가방", "필통", "이어폰", "파우치", "담요", "도자기", "인형")
        cues = ("입대", "전역", "복학", "입학", "진로", "퀴즈", "검진", "확진", "휴가", "생일")
        object_match = sum(word in query and word in memory["title"] for word in objects)
        topic_match = sum(word in query and word in memory["title"] for word in cues)
        return (pair, subject, object_match, topic_match, entity,
                temporal if latest or earliest else "", memory.get("record_kind") == "EVENT", len(terms), temporal)
    return sorted(memories, key=score, reverse=True)
