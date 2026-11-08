"""Select authorized checkup evidence by subject and examination date before ranking text."""

import re
from dataclasses import dataclass
from itertools import zip_longest
from typing import Final

from app.retrieval.chunking import RetrievalChunk

HEALTH_TERMS: Final = r"검진|검사|결과표|혈압|혈색소|헤모글로빈|페리틴|빈혈|저장\s*철분|건강\s*상태|건강\s*(?:은|이|는)?\s*어때"
COMPARISON: Final = r"비교|달라|차이|변화|더\s*전|그\s*전|이전|지난"


@dataclass(frozen=True, slots=True)
class HealthRecord:
    record_id: str
    owner: str
    subjects: tuple[str, ...]
    category: str
    date: str
    title: str
    is_image: bool


def examination_periods(query: str) -> tuple[str, ...]:
    """Carry an explicit year across coordinated months, never across turns."""
    periods: list[str] = []
    year = ""
    for match in re.finditer(r"(?P<iso>(?:19|20)\d{2}-\d{2}(?:-\d{2})?)|(?P<year>(?:19|20)\d{2})\s*년|(?P<month>1[0-2]|0?[1-9])\s*월", query):
        if match["iso"]:
            periods.append(match["iso"])
            year = match["iso"][:4]
        elif match["year"]:
            year = match["year"]
            periods.append(year)
        elif match["month"]:
            prefix = f"{year}-{int(match['month']):02}" if year else f"-{int(match['month']):02}-"
            if periods and periods[-1] == year:
                periods.pop()
            periods.append(prefix)
    return tuple(dict.fromkeys(periods))


def select_health_chunks(query: str, records: list[HealthRecord], chunks: list[RetrievalChunk],
                         subjects: set[str], anchor_ids: set[str]) -> list[RetrievalChunk]:
    """Cover requested dates before filling remaining slots with supporting chunks."""
    eligible = {record.record_id: record for record in records
                if record.category in {"HEALTH_EXAM", "HEALTH_REPORT"}
                and (not subjects or subjects.intersection(record.subjects or (record.owner,)))}
    dated: dict[str, list[RetrievalChunk]] = {}
    for chunk in chunks:
        record = eligible.get(chunk.record_id)
        if record is None:
            continue
        match = re.search(r"\d{4}-\d{2}-\d{2}", record.date)
        if match:
            dated.setdefault(match.group(), []).append(chunk)
    dates = sorted(dated, reverse=True)
    periods = examination_periods(query)
    if periods:
        matching = [date for date in dates if any(date.startswith(period) if not period.startswith("-")
                                                else period in date for period in periods)]
        if len(matching) == 1 and re.search(r"그\s*전|더\s*전|이전|지난", query):
            matching.extend([date for date in dates if date < matching[0]][:1])
        dates = matching
    elif re.search(COMPARISON, query):
        anchors = [date for date, group in dated.items() if any(c.record_id in anchor_ids for c in group)]
        anchor = max(anchors) if anchors else (dates[0] if dates else "")
        dates = [date for date in dates if date <= anchor][:2]
    elif re.search(r"최근|최신|마지막", query):
        dates = dates[:1]
    elif re.search(r"처음|최초|첫", query):
        dates = dates[-1:]
    elif anchor_ids and re.search(r"그때|그\s*사진|그\s*결과|그\s*검사", query):
        dates = [date for date in dates if any(c.record_id in anchor_ids for c in dated[date])]
    metrics = [pattern for pattern in (r"혈압", r"혈색소|헤모글로빈", r"페리틴|저장\s*철분")
               if re.search(pattern, query)]
    groups = []
    for date in dates:
        groups.append(sorted(dated[date], key=lambda chunk: (
            -sum(bool(re.search(metric, chunk.text)) for metric in metrics),
            -eligible[chunk.record_id].is_image, chunk.record_id, chunk.ordinal)))
    # Round-robin keeps a long report from crowding the other comparison date out of k.
    return [chunk for row in zip_longest(*groups) for chunk in row if chunk is not None]
