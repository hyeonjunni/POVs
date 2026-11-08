"""Bounded M4 extraction and review, independent of storage and authorization.

Callers must authorize before supplying bytes and revalidate before publication.
Providers receive no gold labels or trusted person/event identities.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import wave
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from PIL import Image, ImageOps

from app.processing.contracts import OCRResult, ProviderError, STTResult
from app.retrieval.chunking import canonical_json, sha256_text
from app.schemas.multimodal import (
    Annotations,
    FrameAnnotation,
    MediaReviewRequest,
    Segment,
    VisualPrediction,
)

VERSION = "m4-reviewed-media-v1"


class VisualProvider(Protocol):
    extractor_id: str

    def analyze(self, content: bytes, mime_type: str) -> VisualPrediction: ...


@dataclass(frozen=True)
class DecodedMedia:
    duration_ms: int
    audio: bytes | None
    frames: tuple[tuple[int, bytes], ...] = ()


class MediaDecoder(Protocol):
    extractor_id: str

    def decode(self, content: bytes, mime_type: str) -> DecodedMedia: ...


class WaveDecoder:
    extractor_id = "stdlib-wave-v1"

    def decode(self, content: bytes, mime_type: str) -> DecodedMedia:
        if mime_type != "audio/wav":
            raise ProviderError("UNSUPPORTED_MEDIA", "Media decoder is unavailable.")
        try:
            with wave.open(io.BytesIO(content)) as stream:
                if stream.getcomptype() != "NONE" or stream.getframerate() <= 0:
                    raise ValueError()
                frames = stream.getnframes()
                size = frames * stream.getnchannels() * stream.getsampwidth()
                if len(stream.readframes(frames)) != size:
                    raise ValueError()
                duration = frames * 1000 // stream.getframerate()
                if not 0 < duration <= 900000:
                    raise ValueError()
            return DecodedMedia(duration, content)
        except (wave.Error, EOFError, ValueError):
            raise ProviderError("DECODE_ERROR", "Media could not be decoded.") from None


class SyntheticVisualProvider:
    """Explicit inert contract fixture; never a production fallback."""
    extractor_id = "synthetic-visual-v1"

    def analyze(self, content: bytes, mime_type: str) -> VisualPrediction:
        return VisualPrediction(scene="Synthetic scene", objects=["synthetic object"],
                                visual_description="Synthetic visual description")


class SyntheticSpeechProvider:
    extractor_id = "synthetic-speech-v1"

    def transcribe(self, content: bytes, mime_type: str, language: str = "ko") -> STTResult:
        from app.processing.contracts import STTSegment
        return STTResult("Synthetic speech", language, (STTSegment("Synthetic speech", 0, 0.5),))


class SyntheticImageOCR:
    extractor_id = "synthetic-image-ocr-v1"

    def extract(self, content: bytes, mime_type: str) -> OCRResult:
        return OCRResult("", 1.0, "kor+eng")


def _image(content: bytes) -> tuple[bytes, dict[str, Any]]:
    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.width * source.height > 20000000:
                raise ValueError()
            # Preserve the raw EXIF time as a hint; never invent its timezone.
            stamp = source.getexif().get(36867)
            normalized = ImageOps.exif_transpose(source).convert("RGB")
            output = io.BytesIO()
            normalized.save(output, format="PNG")
            return output.getvalue(), {"width": normalized.width, "height": normalized.height,
                                       "exif_recorded_at_hint": stamp if isinstance(stamp, str) else None}
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ProviderError("DECODE_ERROR", "Image could not be decoded.") from None


def _visual(content: bytes, provider: VisualProvider | None) -> VisualPrediction | None:
    if provider is None:
        return None
    return VisualPrediction.model_validate(provider.analyze(content, "image/png"))


def extract(content: bytes, mime_type: str, *, ocr=None, visual=None, speech=None,
            decoder: MediaDecoder | None = None) -> dict[str, Any]:
    family = mime_type.split("/")[0]
    from app.core.config import settings
    limits = {"image": settings.media_image_max_bytes, "audio": settings.media_audio_max_bytes,
              "video": settings.media_video_max_bytes}
    if family not in limits:
        raise ProviderError("UNSUPPORTED_MEDIA", "Unsupported media.")
    if not content or len(content) > limits[family]:
        raise ProviderError("DECODE_ERROR", "Media input is invalid.")
    predicted = Annotations()
    stages: dict[str, str] = {}
    providers: dict[str, str] = {}
    metadata: dict[str, Any] = {}
    duration = None
    audio_hash = None

    def image_annotations(data: bytes):
        normalized, hints = _image(data)
        text, outcome, prediction = "", "OCR_PROCESSING_ERROR", None
        if ocr is None:
            stages["ocr"] = "OCR_ERROR"
        else:
            providers["ocr"] = ocr.extractor_id
            try:
                result = ocr.extract(normalized, "image/png")
                text = result.text
                outcome = "OCR_TEXT_FOUND" if text.strip() else "OCR_EMPTY_SUCCESS"
                stages["ocr"] = "SUCCEEDED" if text.strip() else "VALID_MEDIA_NO_OCR_TEXT"
            except ProviderError:
                stages["ocr"] = "OCR_ERROR"
        if visual is None:
            stages["visual"] = "NOT_CONFIGURED"
        else:
            providers["visual"] = visual.extractor_id
            try:
                prediction = _visual(normalized, visual)
                stages["visual"] = "SUCCEEDED"
            except (ProviderError, ValueError, TypeError):
                stages["visual"] = "VLM_ERROR"
        return text, outcome, prediction, hints

    if family == "image":
        predicted.ocr_text, outcome, predicted.visual, metadata = image_annotations(content)
        metadata["ocr_outcome"] = outcome
    else:
        decoder = decoder or WaveDecoder()
        decoded = decoder.decode(content, mime_type)
        duration = decoded.duration_ms
        if not 0 < duration <= (settings.media_audio_max_duration_ms if family == "audio" else settings.media_video_max_duration_ms):
            raise ProviderError("DECODE_ERROR", "Media duration exceeds limit.")
        if len(decoded.frames) > 12 or any(t < 0 or t >= duration for t, _ in decoded.frames):
            raise ProviderError("DECODE_ERROR", "Invalid media timeline.")
        providers["decoder"] = decoder.extractor_id
        audio_hash = hashlib.sha256(decoded.audio).hexdigest() if decoded.audio is not None else None
        if decoded.audio is None:
            stages["speech"] = "VALID_MEDIA_NO_SPEECH"
        elif speech is None:
            stages["speech"] = "NOT_CONFIGURED"
        else:
            providers["speech"] = speech.extractor_id
            try:
                result = speech.transcribe(decoded.audio, "audio/wav", language="ko")
                predicted.language = result.language
                predicted.transcript = result.text
                predicted.segments = [Segment(segment_id=uuid4(), text=s.text,
                    start_ms=round(s.start_seconds * 1000), end_ms=round(s.end_seconds * 1000))
                    for s in result.segments]
                if any(s.end_ms > duration for s in predicted.segments):
                    raise ValueError()
                if result.text.strip() and not predicted.segments:
                    raise ValueError()
                stages["speech"] = "SUCCEEDED" if result.text.strip() else "VALID_MEDIA_NO_SPEECH"
            except (ProviderError, ValueError, TypeError):
                predicted.transcript, predicted.segments = "", []
                stages["speech"] = "STT_ERROR"
        for frame_index, (stamp, data) in enumerate(decoded.frames):
            text, outcome, prediction, _ = image_annotations(data)
            for stage in ("ocr", "visual"):
                stages[f"frame_{frame_index}_{stage}"] = stages.pop(stage)
            predicted.frames.append(FrameAnnotation(frame_id=uuid4(), timestamp_ms=stamp,
                image_sha256=hashlib.sha256(data).hexdigest(), ocr_outcome=outcome,
                ocr_text=text, visual=prediction))
    failed = [name for name, state in stages.items() if state.endswith("ERROR") or state == "NOT_CONFIGURED"]
    value = {"version": VERSION, "media_type": family.upper(), "review_state": "PENDING_REVIEW",
        "review_version": 0, "predicted": predicted.model_dump(mode="json"), "approved": None,
        "trusted_metadata": {}, "gold": None, "duration_ms": duration, "media_metadata": metadata,
        "audio_track_sha256": audio_hash, "stages": stages, "providers": providers,
        "coverage": "PARTIAL" if failed else "COMPLETE", "excluded_stages": failed,
        "source_sha256": hashlib.sha256(content).hexdigest()}
    return {"text": "", "extractor": "AUTO_OCR" if family == "image" else "AUTO_STT",
        "extractor_id": VERSION, "user_confirmed": False, "review_status": "unreviewed", "m4": value}


def eligible(result: dict[str, Any]) -> bool:
    media = result.get("m4")
    return (isinstance(media, dict) and media.get("version") == VERSION
            and media.get("review_state") == "APPROVED" and media.get("approved") is not None)


def review(result: dict[str, Any], request: MediaReviewRequest, attempt: int) -> dict[str, Any]:
    """Pure CAS transform. The storage caller checks reviewer authority and IDs."""
    if request.source_attempt != attempt or request.source_result_sha256 != sha256_text(canonical_json(result)):
        raise ValueError("STALE_SOURCE")
    value = copy.deepcopy(result)
    media = value["m4"]
    if request.review_version != media["review_version"]:
        raise ValueError("REVISION_CONFLICT")
    if request.action != "reject" and set(request.excluded_stages) != set(media["excluded_stages"]):
        raise ValueError("Partial review must explicitly acknowledge excluded stages")
    media["approved_excluded_stages"] = list(request.excluded_stages)
    approved = request.approved or Annotations.model_validate(media["predicted"])
    original = Annotations.model_validate(media["predicted"])
    if (media["media_type"] != "IMAGE" and (approved.ocr_text or approved.visual is not None)
            or media["media_type"] == "IMAGE" and (approved.transcript or approved.segments or approved.frames)
            or media["media_type"] == "AUDIO" and approved.frames):
        raise ValueError("Annotations must match the source media type")
    if ([s.model_dump(exclude={"text"}) for s in approved.segments] !=
            [s.model_dump(exclude={"text"}) for s in original.segments]
            or [(f.frame_id, f.timestamp_ms, f.image_sha256) for f in approved.frames] !=
            [(f.frame_id, f.timestamp_ms, f.image_sha256) for f in original.frames]):
        raise ValueError("Source anchors cannot be rewritten")
    media["review_version"] += 1
    media["review_state"] = "APPROVED" if request.action != "reject" else "PENDING_REVIEW"
    media["failure_reason"] = "REVIEW_REJECTED" if request.action == "reject" else None
    media["approved"] = approved.model_dump(mode="json") if request.action != "reject" else None
    media["trusted_metadata"] = request.metadata.model_dump(mode="json")
    pieces: list[str] = []
    anchors: list[dict[str, Any]] = []
    def append(text: str, **anchor):
        if not text.strip():
            return
        start = sum(len(t) + 1 for t in pieces)
        pieces.append(text)
        anchors.append(dict(char_start=start, char_end=start + len(text), **anchor))
    if request.action != "reject":
        append(approved.ocr_text, kind="IMAGE", field="ocr")
        if approved.visual:
            append(approved.visual.visual_description, kind="IMAGE", field="visual")
        if approved.transcript.strip():
            from app.retrieval.chunking import normalize_text
            if normalize_text(approved.transcript) != normalize_text(" ".join(s.text for s in approved.segments)):
                raise ValueError("Transcript must match its reviewed segments")
            for segment in approved.segments:
                append(segment.text, kind="AUDIO", segment_id=str(segment.segment_id),
                       start_ms=segment.start_ms, end_ms=segment.end_ms)
        for frame in approved.frames:
            for field, content in [("ocr", frame.ocr_text),
                                   ("visual", frame.visual.visual_description if frame.visual else "")]:
                append(content, kind="VIDEO", field=field, frame_id=str(frame.frame_id),
                       frame_ms=frame.timestamp_ms)
    value["text"] = "\n".join(pieces)
    media["anchors"] = anchors
    # Bound every persisted representation, including client corrections.
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 900000:
        raise ValueError("Annotation output exceeds limit")
    return value


class SyntheticVideoDecoder:
    """Only the explicit test marker, never a decoder for real video bytes."""
    extractor_id = "synthetic-video-contract-v1"
    marker = b"\x00\x00\x00\x18ftypmp42M4-SYNTHETIC-CONTRACT-ONLY"

    def decode(self, content: bytes, mime_type: str) -> DecodedMedia:
        if mime_type != "video/mp4" or content != self.marker:
            raise ProviderError("UNSUPPORTED_MEDIA", "Synthetic adapter only accepts its fixture.")
        image = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(image, format="PNG")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 16000)
        return DecodedMedia(1000, audio.getvalue(), ((0, image.getvalue()), (500, image.getvalue())))
