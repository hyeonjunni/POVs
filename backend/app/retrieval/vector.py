from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from numbers import Real

from app.models.retrieval import M2_EMBEDDING_DIMENSIONS
from app.retrieval.ranking import ChunkLike, RankedChunk


def _validated(vector: Sequence[float]) -> list[float]:
    if (
        not isinstance(vector, Sequence)
        or isinstance(vector, (str, bytes))
        or len(vector) != M2_EMBEDDING_DIMENSIONS
        or any(isinstance(value, bool) or not isinstance(value, Real) for value in vector)
    ):
        raise ValueError("Invalid vector")
    try:
        values = [float(value) for value in vector]
    except OverflowError:
        raise ValueError("Invalid vector") from None
    if not all(math.isfinite(value) for value in values) or not any(values):
        raise ValueError("Invalid vector")
    return values


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    left_values, right_values = _validated(left), _validated(right)
    left_scale, right_scale = max(map(abs, left_values)), max(map(abs, right_values))
    left_scaled = [value / left_scale for value in left_values]
    right_scaled = [value / right_scale for value in right_values]
    score = math.fsum(a * b for a, b in zip(left_scaled, right_scaled)) / math.sqrt(
        math.fsum(a * a for a in left_scaled) * math.fsum(b * b for b in right_scaled)
    )
    if not math.isfinite(score):
        raise ValueError("Invalid vector")
    return score


def rank_vector(
    rows: Iterable[tuple[ChunkLike, Sequence[float]]], query_vector: Sequence[float]
) -> list[RankedChunk]:
    _validated(query_vector)
    ranked: list[RankedChunk] = []
    for chunk, embedding in rows:
        try:
            score = cosine_similarity(query_vector, embedding)
        except ValueError:
            continue
        ranked.append(RankedChunk(chunk=chunk, score=score, cosine=score, arms=frozenset({"vector"})))
    return sorted(ranked, key=lambda result: (-result.score, result.chunk.id))
