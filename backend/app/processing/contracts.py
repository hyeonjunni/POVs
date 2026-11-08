"""OCR/STT provider contracts (Milestone 1 Task 5).

`ProviderError.code` is a stable, public-safe identifier; `message` is a short,
safe-to-surface string. Neither ever carries a stack trace, raw stderr, source
bytes, or private extracted content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OCRRegion:
    """Local image OCR boxes: EXIF-normalized, unscaled pixels, top-left origin."""

    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class OCRResult:
    text: str
    confidence: float
    language: str
    regions: tuple[OCRRegion, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class STTSegment:
    text: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class STTResult:
    text: str
    language: str
    segments: tuple[STTSegment, ...] = field(default_factory=tuple)


class OCRProvider(Protocol):
    def extract(self, content: bytes, mime_type: str) -> OCRResult: ...


class STTProvider(Protocol):
    def transcribe(self, content: bytes, mime_type: str, language: str = "ko") -> STTResult: ...
