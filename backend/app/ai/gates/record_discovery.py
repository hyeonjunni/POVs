import json
import re
from dataclasses import replace

from collections.abc import Collection

from app.ai.gates.entity_index import KINSHIP, RoomEntityIndex
from app.ai.gates.textrules import fold_family_terms
from app.retrieval.admission import admit_candidates
from app.retrieval.ranking import RankedChunk, rank_substring


def audio_discovery(query: str) -> bool:
    return re.search(r"(?<![가-힣])(?:음성|오디오|녹음)(?=\s|[을를]|$)", query) is not None


def discovery_query(query: str) -> str | None:
    match = re.fullmatch(r"(.+?)\s+(?:보여줘|찾아줘)[.!?？]*", query.strip())
    if match is None:
        return None
    topic = re.sub(r"\s+(?:관련\s+)?기록(?:을|들|들을)?$", "", match[1]).strip()
    if re.search(r"언제|어디|몇\s*(?:번|회)|누구|누가|어떻게|가장|최근|모두", topic):
        return None
    return topic or None


def referenced_people(text: str, names: Collection[str] = ()) -> set[str]:
    surfaces = {fold_family_terms(name) for name in (*names, *KINSHIP)}
    pattern = r"(?<![가-힣])(" + "|".join(re.escape(s) for s in sorted(surfaces, key=lambda s: (-len(s), s)))
    pattern += r")(?=(?:은|는|이|가|을|를|의|에게|께서|에)?(?:\s|[?？,.]|$))"
    return set(re.findall(pattern, fold_family_terms(text)))


def discovery_candidates(hits: list[RankedChunk], query: str,
                         index: RoomEntityIndex, *, subject_bound: bool = False) -> list[RankedChunk]:
    if audio_discovery(query):
        hits = [hit for hit in hits if json.loads(hit.chunk.source_locator_json).get("media_type") == "AUDIO"]
    persons = referenced_people(query, index.persons)
    if persons and not subject_bound:
        hits = [hit for hit in hits if persons == referenced_people(hit.chunk.text, index.persons)]
    topic = discovery_topic(query, index.persons)
    if audio_discovery(query) and not topic and (subject_bound or not persons):
        return hits
    if topic != fold_family_terms(query).strip() or persons:
        hits = [replace(hit, bm25=None) for hit in hits]
    literal_scores = {hit.chunk.id: hit.score for hit in rank_substring([hit.chunk for hit in hits], topic)}
    hits = [replace(hit, score=literal_scores[hit.chunk.id], arms=hit.arms | {"substring"})
            if hit.chunk.id in literal_scores else hit for hit in hits]
    return admit_candidates(hits, topic, index)


def discovery_topic(query: str, names: Collection[str] = ()) -> str:
    topic = fold_family_terms(query)
    for person in sorted(referenced_people(query, names), key=len, reverse=True):
        topic = re.sub(r"(?<![가-힣])" + re.escape(person)
                       + r"(?:은|는|이|가|을|를|의|에게|께서|에)?(?=\s|[?？,.]|$)", " ", topic)
    if audio_discovery(query):
        topic = re.sub(r"(?<![가-힣])(?:음성|오디오|녹음)(?:을|를)?(?=\s|$)", " ", topic)
        topic = re.sub(r"(?<![가-힣])(?:말한|남긴|대한|관한|관련)(?=\s|$)", " ", topic)
        return " ".join(topic.split())
    return topic.strip() or query


def distinct_records(hits: list[RankedChunk]) -> list[RankedChunk]:
    records = {}
    for hit in hits:
        records.setdefault(hit.chunk.record_id, hit)
    return list(records.values())
