"""Local, command-backed STT provider (Whisper-compatible CLI, JSON output).

Invokes a server-configured executable via `subprocess.run` with an argument
array (`shell=False`), a bounded timeout, and a temporary working directory
that is always cleaned up. Never logs source bytes, transcribed text, or raw
stderr — only stable, safe `ProviderError` codes/messages.
"""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path

from app.core.config import settings
from app.processing.contracts import ProviderError, STTResult, STTSegment

_MIME_SUFFIXES = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
}


def _suffix_for_mime(mime_type: str) -> str:
    return _MIME_SUFFIXES.get(mime_type, ".wav")


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name!r} is not allowed")


def _finite_seconds(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("segment times must be numbers")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("segment times must be finite and non-negative")
    return seconds


def _parse_whisper_json(json_path: Path) -> STTResult:
    data = json.loads(json_path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    if not isinstance(data, dict) or not isinstance(data.get("text", ""), str):
        raise ValueError("STT output must be an object with a string 'text'")
    text = data.get("text", "").strip()
    if not text:
        raise ValueError("STT output contained no transcript")

    raw_segments = data.get("segments", [])
    if not isinstance(raw_segments, list):
        raise ValueError("'segments' must be a list")
    segments: list[STTSegment] = []
    previous_end = 0.0
    for raw in raw_segments:
        if not isinstance(raw, dict) or not isinstance(raw.get("text", ""), str):
            raise ValueError("each segment must be an object with a string 'text'")
        start, end = _finite_seconds(raw.get("start", 0.0)), _finite_seconds(raw.get("end", 0.0))
        # Non-overlapping and forward-ordered: a segment may not start before the previous one ended.
        if end < start or start < previous_end:
            raise ValueError("segments must be ordered with end >= start")
        previous_end = end
        segments.append(STTSegment(text=raw.get("text", "").strip(), start_seconds=start, end_seconds=end))

    language = data.get("language") or "ko"
    if not isinstance(language, str):
        raise ValueError("'language' must be a string")
    return STTResult(text=text, language=language, segments=tuple(segments))


class LocalWhisperSTTProvider:
    def __init__(self, executable: str | None = None, timeout_seconds: int | None = None, language: str | None = None):
        self.executable = executable if executable is not None else settings.stt_executable_path
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.stt_timeout_seconds
        self.language = language if language is not None else settings.stt_language

    @property
    def extractor_id(self) -> str:
        return f"local_whisper:{Path(self.executable).name or 'unconfigured'}"

    def transcribe(self, content: bytes, mime_type: str, language: str | None = None) -> STTResult:
        language = language or self.language
        if not self.executable:
            raise ProviderError("stt_not_configured", "No local STT executable is configured.")

        with tempfile.TemporaryDirectory(prefix="ambient-stt-") as raw_tmp_dir:
            tmp_dir = Path(raw_tmp_dir)
            input_path = tmp_dir / f"input{_suffix_for_mime(mime_type)}"
            input_path.write_bytes(content)

            try:
                completed = subprocess.run(
                    [
                        self.executable,
                        str(input_path),
                        "--language", language,
                        "--output_format", "json",
                        "--output_dir", str(tmp_dir),
                    ],
                    shell=False,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise ProviderError("stt_timeout", "STT command timed out.") from error
            except OSError as error:
                raise ProviderError("stt_not_configured", "The configured STT executable could not be run.") from error

            if completed.returncode != 0:
                raise ProviderError("stt_nonzero_exit", "STT command exited with an error.")

            json_path = input_path.with_suffix(".json")
            if not json_path.exists():
                raise ProviderError("stt_malformed_output", "STT command produced no output.")
            # Bounded outside the parse `try`, so the bound is not reported as a parse failure.
            if json_path.stat().st_size > settings.processing_max_output_bytes:
                raise ProviderError("stt_output_too_large", "STT output exceeded the size limit.")

            try:
                return _parse_whisper_json(json_path)
            except Exception as error:
                raise ProviderError("stt_malformed_output", "STT output could not be parsed.") from error
