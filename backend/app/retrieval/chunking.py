from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

CHUNKER_VERSION = "ws160-o32-v1"
_NAMESPACE = uuid.UUID("13e5a36d-a6f6-4b59-94ed-7a59424438f2")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


@dataclass(frozen=True)
class ChunkSource:
    room_id: str
    record_id: str
    file_id: str
    processing_job_id: str
    source_attempt: int
    result: Mapping[str, object]
    chunker_version: str = CHUNKER_VERSION
    canonical_result_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        snapshot = canonical_json(self.result)
        object.__setattr__(self, "result", _freeze(json.loads(snapshot)))
        object.__setattr__(self, "canonical_result_json", snapshot)


@dataclass(frozen=True)
class RetrievalChunk:
    id: str
    room_id: str
    record_id: str
    file_id: str
    processing_job_id: str
    source_attempt: int
    source_result_sha256: str
    ordinal: int
    text: str
    text_sha256: str
    token_count: int
    char_start: int
    char_end: int
    source_locator_json: str
    extractor: str
    extractor_id: str
    review_status: str
    user_confirmed: bool
    chunker_version: str


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in re.finditer(r"\S+", text, re.UNICODE):
        normalized = normalize_text(match.group())
        tokens.extend(_Token(part, match.start(), match.end()) for part in normalized.split())
    return tokens


def _structured_spans(
    result: Mapping[str, object], tokens: list[_Token]
) -> tuple[str, list[tuple[int, int, object]]] | None:
    normalized = " ".join(token.text for token in tokens)
    entries = result.get("regions")
    kind = "ocr_regions"
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        entries = result.get("segments")
        kind = "stt_segments"
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)) or not entries:
        return None

    normalized_offsets: list[tuple[int, int]] = []
    cursor = 0
    for token in tokens:
        normalized_offsets.append((cursor, cursor + len(token.text)))
        cursor += len(token.text) + 1

    needles: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("text"), str):
            return None
        needle = normalize_text(entry["text"])
        if not needle:
            return None
        needles.append(needle)

    alignment: list[tuple[int, int]] = []
    cursor = 0
    for needle in needles:
        start = normalized.find(needle, cursor)
        if start < 0:
            return None
        end = start + len(needle)
        alignment.append((start, end))
        cursor = end

    spans: list[tuple[int, int, object]] = []
    for index, ((start, end), entry) in enumerate(zip(alignment, entries)):
        covered = [
            i
            for i, (token_start, token_end) in enumerate(normalized_offsets)
            if token_start < end and token_end > start
        ]
        if not covered:
            return None
        payload: object = index
        if kind == "stt_segments":
            payload = {
                "index": index,
                "start_seconds": entry.get("start_seconds"),
                "end_seconds": entry.get("end_seconds"),
            }
        spans.append((tokens[covered[0]].start, tokens[covered[-1]].end, payload))
    return kind, spans


def _locator(
    structured: tuple[str, list[tuple[int, int, object]]] | None,
    start: int,
    end: int,
) -> str:
    if structured is None:
        return canonical_json({"kind": "full_text"})
    kind, spans = structured
    matches = [payload for left, right, payload in spans if left < end and right > start]
    if not matches:
        return canonical_json({"kind": "full_text"})
    key = "indexes" if kind == "ocr_regions" else "segments"
    return canonical_json({"kind": kind, key: matches})


def chunk_processing_result(source: ChunkSource) -> tuple[RetrievalChunk, ...]:
    raw_text = source.result.get("text")
    if not isinstance(raw_text, str):
        return ()
    tokens = _tokens(raw_text)
    if not tokens:
        return ()

    result_hash = sha256_text(source.canonical_result_json)
    structured = _structured_spans(source.result, tokens)
    chunks: list[RetrievalChunk] = []
    for ordinal, first in enumerate(range(0, len(tokens), 128)):
        window = tokens[first : first + 160]
        if not window:
            break
        text = " ".join(token.text for token in window)
        text_hash = sha256_text(text)
        chunk_id = str(
            uuid.uuid5(
                _NAMESPACE,
                canonical_json(
                    [
                        source.processing_job_id,
                        source.source_attempt,
                        source.chunker_version,
                        ordinal,
                        text_hash,
                    ]
                ),
            )
        )
        chunks.append(
            RetrievalChunk(
                id=chunk_id,
                room_id=source.room_id,
                record_id=source.record_id,
                file_id=source.file_id,
                processing_job_id=source.processing_job_id,
                source_attempt=source.source_attempt,
                source_result_sha256=result_hash,
                ordinal=ordinal,
                text=text,
                text_sha256=text_hash,
                token_count=len(window),
                char_start=window[0].start,
                char_end=window[-1].end,
                source_locator_json=_media_locator(source.result, window[0].start, window[-1].end)
                    if "m4" in source.result else _locator(structured, window[0].start, window[-1].end),
                extractor=str(source.result.get("extractor", "")),
                extractor_id=str(source.result.get("extractor_id", "")),
                review_status=str(source.result.get("review_status", "unreviewed")),
                user_confirmed=source.result.get("user_confirmed") is True,
                chunker_version=source.chunker_version,
            )
        )
        if first + 160 >= len(tokens):
            break
    return tuple(chunks)


def _media_locator(result, start, end):
    media = result["m4"]
    anchors = [dict(a) for a in media.get("anchors", ())
               if a["char_start"] < end and a["char_end"] > start]
    return canonical_json({"kind": "multimodal", "media_type": media["media_type"],
        "review_version": media["review_version"], "anchors": anchors})
