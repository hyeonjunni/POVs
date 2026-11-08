from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.retrieval.chunking import normalize_text


class ChunkLike(Protocol):
    id: str
    text: str


@dataclass(frozen=True)
class RankedChunk:
    chunk: ChunkLike
    score: float
    cosine: float | None = None
    bm25: float | None = None
    arms: frozenset[str] = frozenset()


def _substring_normalize(text: str) -> str:
    return normalize_text(text)


def _bm25_tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold(), re.UNICODE)


def rank_substring(chunks: Sequence[ChunkLike], query: str) -> list[RankedChunk]:
    needle = _substring_normalize(query)
    if not needle:
        return []
    ranked = [
        RankedChunk(chunk=chunk, score=float(_substring_normalize(chunk.text).count(needle)), arms=frozenset({"substring"}))
        for chunk in chunks
    ]
    return sorted((result for result in ranked if result.score), key=lambda result: (-result.score, result.chunk.id))


def rank_bm25(chunks: Sequence[ChunkLike], query: str) -> list[RankedChunk]:
    query_tokens = _bm25_tokens(query)
    if not chunks or not query_tokens:
        return []

    document_terms = [Counter(_bm25_tokens(chunk.text)) for chunk in chunks]
    average_length = sum(sum(terms.values()) for terms in document_terms) / len(chunks)
    if not average_length:
        return []
    document_frequency = Counter(
        token for terms in document_terms for token in terms
    )
    document_count = len(chunks)
    ranked: list[RankedChunk] = []
    for chunk, terms in zip(chunks, document_terms):
        document_length = sum(terms.values())
        score = 0.0
        for token in query_tokens:
            term_frequency = terms[token]
            if not term_frequency:
                continue
            inverse_document_frequency = math.log(
                1 + (document_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5)
            )
            score += inverse_document_frequency * term_frequency * 2.2 / (
                term_frequency + 1.2 * (1 - 0.75 + 0.75 * document_length / average_length)
            )
        if score:
            ranked.append(RankedChunk(chunk=chunk, score=score, bm25=score, arms=frozenset({"bm25"})))
    return sorted(ranked, key=lambda result: (-result.score, result.chunk.id))


def fuse_rrf(
    bm25: Sequence[tuple[ChunkLike, float]], vector: Sequence[tuple[ChunkLike, float]], *, rrf_k: int = 60
) -> list[RankedChunk]:
    ranks: dict[str, tuple[ChunkLike, int | None, int | None]] = {}
    for arm, position in ((bm25, 1), (vector, 2)):
        for rank, (chunk, _score) in enumerate(arm, start=1):
            prior = ranks.get(chunk.id, (chunk, None, None))
            if position == 1 and prior[1] is None:
                ranks[chunk.id] = (chunk, rank, prior[2])
            elif position == 2 and prior[2] is None:
                ranks[chunk.id] = (chunk, prior[1], rank)
    lexical_scores = {chunk.id: score for chunk, score in reversed(bm25)}
    vector_scores = {chunk.id: score for chunk, score in reversed(vector)}
    fused = [
        (RankedChunk(chunk=chunk, score=sum(1 / (rrf_k + rank) for rank in (bm25_rank, vector_rank) if rank), cosine=vector_scores.get(chunk.id), bm25=lexical_scores.get(chunk.id),
                     arms=frozenset(arm for arm, rank in (("bm25", bm25_rank), ("vector", vector_rank)) if rank)), min(rank for rank in (bm25_rank, vector_rank) if rank))
        for chunk, bm25_rank, vector_rank in ranks.values()
    ]
    return [result for result, _best_rank in sorted(fused, key=lambda item: (-item[0].score, item[1], item[0].chunk.id))]
