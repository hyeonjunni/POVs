"""Explicit local media tools. No network, shell, arbitrary executable or cloud fallback."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from app.processing.contracts import ProviderError
from app.processing.local_ocr import _parse
from app.processing.multimodal import DecodedMedia
from app.processing.ocr_preprocessing import prepare_ocr_image


def private_root(root: Path) -> Path:
    info = root.lstat()
    if not root.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProviderError("DECODE_ERROR", "Private processing directory unavailable.")
    return root


def command(binary: str, args: list[str], *, timeout: int = 30) -> bytes:
    path = Path(binary)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise OSError()
        result = subprocess.run([binary, *args], shell=False, timeout=timeout, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        if result.returncode or len(result.stdout) > 1000000:
            raise OSError()
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise ProviderError("DECODE_ERROR", "Local media processing unavailable.") from None


class LocalImageOCR:
    extractor_id = "m4-tesseract-kor-eng-v1"

    def __init__(self, root: Path):
        self.root = private_root(root)

    def extract(self, content: bytes, mime_type: str):
        with tempfile.TemporaryDirectory(prefix="m4-ocr-", dir=private_root(self.root)) as raw:
            directory = Path(raw)
            source, output = directory / "input.png", directory / "output"
            working = prepare_ocr_image(content, source)
            try:
                command("/usr/bin/tesseract", [str(source), str(output), "-l", "kor+eng", "tsv", "txt"])
                tsv, txt = output.with_suffix(".tsv"), output.with_suffix(".txt")
                if tsv.stat().st_size + txt.stat().st_size > 1000000:
                    raise ValueError()
                return working.normalized_regions(_parse(tsv, txt, "kor+eng", allow_empty=True))
            except (OSError, ValueError, ProviderError):
                raise ProviderError("OCR_ERROR", "OCR processing failed.") from None


class LocalMediaDecoder:
    """Bounded sparse sampling, mono PCM extraction; no derived plaintext survives."""
    extractor_id = "m4-ffmpeg-sparse-v1"

    def __init__(self, root: Path):
        self.root = private_root(root)

    def decode(self, content: bytes, mime_type: str) -> DecodedMedia:
        if mime_type not in ("video/mp4", "audio/mpeg", "audio/wav"):
            raise ProviderError("UNSUPPORTED_MEDIA", "Unsupported media.")
        with tempfile.TemporaryDirectory(prefix="m4-media-", dir=private_root(self.root)) as raw:
            directory = Path(raw)
            source = directory / "input"
            source.write_bytes(content)
            try:
                probe = json.loads(command("/usr/bin/ffprobe", ["-v", "error", "-protocol_whitelist", "file,pipe",
                    "-show_format", "-show_streams", "-of", "json", str(source)]))
                duration = round(float(probe["format"]["duration"]) * 1000)
                from app.core.config import settings
                limit = settings.media_video_max_duration_ms if mime_type.startswith("video/") else settings.media_audio_max_duration_ms
                if not 0 < duration <= limit:
                    raise ValueError()
                streams = probe["streams"]
                audio = None
                if any(s.get("codec_type") == "audio" for s in streams):
                    output = directory / "audio.wav"
                    command("/usr/bin/ffmpeg", ["-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
                        "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                        "-t", str(limit / 1000), "-fs", "30000000", str(output)], timeout=60)
                    if output.stat().st_size > 30000000:
                        raise ValueError()
                    audio = output.read_bytes()
                frames = []
                if mime_type.startswith("video/"):
                    if not any(s.get("codec_type") == "video" for s in streams):
                        raise ValueError()
                    for index, timestamp in enumerate(sorted({0, duration // 2, max(0, duration - 100)})):
                        frame = directory / f"frame-{index}.png"
                        command("/usr/bin/ffmpeg", ["-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
                            "-ss", str(timestamp / 1000), "-i", str(source), "-frames:v", "1",
                            "-vf", "scale=640:640:force_original_aspect_ratio=decrease", str(frame)])
                        if frame.stat().st_size > 4000000:
                            raise ValueError()
                        frames.append((timestamp, frame.read_bytes()))
                return DecodedMedia(duration, audio, tuple(frames))
            except (OSError, ValueError, KeyError, TypeError):
                raise ProviderError("DECODE_ERROR", "Media could not be decoded.") from None
