"""Per-candidate relevance, independent of rank and client display depth."""
from app.core.demo_trace import emit
import math
from dataclasses import replace
from app.retrieval.ranking import rank_bm25

from app.ai.gates.answer_router import (
    _chunk_events, _event_signature, evidence_scope_matches,
)


def consistent(query, text, index):
    return evidence_scope_matches(query, text, index)


def admit_candidates(hits, query, index, *, vector_floor=None):
    if vector_floor is None:
        from app.core.config import settings
        vector_floor = settings.retrieval_vector_relevance_floor
    if not math.isfinite(vector_floor) or not 0 <= vector_floor <= 1:
        raise ValueError('Invalid vector relevance floor')
    # Vector-only ranking still permits independently measured lexical support.
    lexical_scores = {h.chunk.id: h.score for h in rank_bm25([h.chunk for h in hits], query)}
    hits = [replace(h, bm25=lexical_scores.get(h.chunk.id, 0.0)) if h.bm25 is None else h
            for h in hits]
    def supported(hit):
        lexical = (hit.bm25 is not None and math.isfinite(hit.bm25) and hit.bm25 > 0)
        substring = 'substring' in hit.arms and math.isfinite(hit.score) and hit.score > 0
        vector = (hit.cosine is not None and math.isfinite(hit.cosine)
                  and hit.cosine >= vector_floor)
        return (lexical or substring or vector) and consistent(query, hit.chunk.text, index)
    admitted = []
    emit("RETRIEVAL", candidate_count=len(hits))
    for alias, hit in enumerate(hits, 1):
        accepted = supported(hit)
        emit("RELEVANCE GATE", alias=alias, cosine=hit.cosine, bm25=hit.bm25,
             floor=vector_floor, decision="ADMIT" if accepted else "REJECT",
             source_type="TEXT" if hit.chunk.extractor == "NATIVE_TEXT" else None)
        if accepted:
            admitted.append(hit)
    emit("RELEVANCE GATE", admitted_evidence=len(admitted), floor=vector_floor)
    return admitted


def safety_related(query, text, index):
    """Lexical/event support for veto-only evidence outside the ranked pool."""
    if not consistent(query, text, index):
        return False
    query_events, text_events = _chunk_events(query, index), _chunk_events(text, index)
    left, right = _event_signature(query_events), _event_signature(text_events)
    return bool(query_events & text_events or any(a & b for a, b in zip(left, right)))
