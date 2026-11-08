from __future__ import annotations

import math
from collections.abc import Sequence
from numbers import Real
from typing import Protocol

import httpx


class EmbeddingProvider(Protocol):
    model_key: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingProviderError(RuntimeError):
    pass


def validate_embeddings(
    vectors: object, *, expected_count: int, dimensions: int
) -> list[list[float]]:
    if not isinstance(vectors, Sequence) or isinstance(vectors, (str, bytes)):
        raise EmbeddingProviderError("Embedding provider returned an invalid response")
    if len(vectors) != expected_count:
        raise EmbeddingProviderError("Embedding provider returned an invalid response")

    validated: list[list[float]] = []
    for vector in vectors:
        if (
            not isinstance(vector, Sequence)
            or isinstance(vector, (str, bytes))
            or len(vector) != dimensions
        ):
            raise EmbeddingProviderError("Embedding provider returned an invalid response")
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in vector):
            raise EmbeddingProviderError("Embedding provider returned an invalid response")
        try:
            values = [float(value) for value in vector]
        except OverflowError:
            raise EmbeddingProviderError(
                "Embedding provider returned an invalid response"
            ) from None
        if not all(math.isfinite(value) for value in values) or not any(values):
            raise EmbeddingProviderError("Embedding provider returned an invalid response")
        validated.append(values)
    return validated


class HTTPEmbeddingProvider:
    def __init__(
        self,
        *,
        endpoint_url: str,
        model_key: str,
        dimensions: int,
        batch_size: int,
        timeout_seconds: int,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.model_key = model_key
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.endpoint_url.strip() or not self.model_key.strip():
            raise EmbeddingProviderError("Embedding provider is not configured")
        if self.batch_size < 1 or self.timeout_seconds < 1:
            raise EmbeddingProviderError("Embedding provider is not configured")

        embeddings: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start : start + self.batch_size])
                response = httpx.post(
                    self.endpoint_url,
                    json={"model": self.model_key, "input": batch},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("model") != self.model_key:
                    raise EmbeddingProviderError(
                        "Embedding provider returned an invalid response"
                    )
                data = payload.get("data")
                raw = (
                    [item.get("embedding") for item in data]
                    if isinstance(data, list) and all(isinstance(item, dict) for item in data)
                    else None
                )
                embeddings.extend(
                    validate_embeddings(
                        raw, expected_count=len(batch), dimensions=self.dimensions
                    )
                )
        except EmbeddingProviderError:
            raise
        except (httpx.HTTPError, TypeError, ValueError, KeyError):
            raise EmbeddingProviderError("Embedding provider failed") from None
        return embeddings
