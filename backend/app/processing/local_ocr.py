"""Local, command-backed OCR provider (Tesseract native TXT + TSV output).

Invokes a server-configured executable via `subprocess.run` with an argument
array (`shell=False`), a bounded timeout, and a temporary working directory
that is always cleaned up. A single invocation requests both `tsv` and `txt`:
the native Tesseract TXT supplies the extracted text (its spacing is preserved
verbatim, with no token correction, fuzzy edit, Unicode normalization, or
internal spacing/line-break stripping), while the TSV supplies confidence,
regions, order and geometry only. The two outputs are validated by exact
non-whitespace character-sequence equality before the result is returned;
inconsistent, missing, empty, invalid-UTF8 or oversized output fails closed.
Never logs source bytes, extracted text, or raw stderr — only stable, safe
`ProviderError` codes/messages.
"""
from __future__ import annotations

import csv
import math
import subprocess
import tempfile
from pathlib import Path

from app.core.config import settings
from app.processing.contracts import OCRRegion, OCRResult, ProviderError
from app.processing.ocr_preprocessing import prepare_ocr_image

_MIME_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}


def _suffix_for_mime(mime_type: str) -> str:
    return _MIME_SUFFIXES.get(mime_type, ".png")


def _finite_non_negative_int(raw: str | None) -> int:
    value = float(raw or 0)
    if not math.isfinite(value) or value < 0:
        raise ValueError("OCR geometry must be a finite non-negative number")
    return int(value)


def _non_whitespace(text: str) -> str:
    """Non-whitespace character sequence, used only for the equality comparison
    below — never used to rewrite or reconstruct the text."""
    return "".join(char for char in text if not char.isspace())


def _parse(tsv_path: Path, txt_path: Path, language: str, *, allow_empty: bool = False) -> OCRResult:
    regions: list[OCRRegion] = []
    words: list[str] = []
    confidences: list[float] = []

    with tsv_path.open("r", encoding="utf-8", newline="") as handle:
        # Tesseract emits literal TSV, not CSV-quoted fields. Recognized
        # quotes are text; interpreting them as syntax can swallow later rows.
        reader = csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            confidence = float(row.get("conf", "-1"))   # ValueError propagates
            if not math.isfinite(confidence) or confidence > 100:
                raise ValueError("OCR confidence must be finite and <= 100")
            if confidence < 0:                          # Tesseract marks non-word rows with -1
                continue
            words.append(text)
            confidences.append(confidence)
            regions.append(
                OCRRegion(
                    text=text,
                    confidence=round(confidence / 100.0, 4),
                    left=_finite_non_negative_int(row.get("left")),
                    top=_finite_non_negative_int(row.get("top")),
                    width=_finite_non_negative_int(row.get("width")),
                    height=_finite_non_negative_int(row.get("height")),
                )
            )

    if not words:
        if allow_empty and not txt_path.read_text(encoding="utf-8").strip():
            return OCRResult("", 0.0, language)
        raise ValueError("OCR output contained no recognizable text")

    # Native Tesseract TXT is the engine's own text; TSV carries only
    # confidence/geometry. The two must agree on their non-whitespace sequence;
    # otherwise the output is inconsistent and we fail closed (no guessed
    # alignment, no fallback to a second provider). The comparison never rewrites
    # the text: the returned text is the native TXT exactly as produced.
    # newline="" keeps native CRLF/CR verbatim (read_text would apply
    # universal-newline translation); decoding is still strict UTF-8, so
    # invalid bytes raise and fail closed. The text is never rewritten.
    native_text = txt_path.open("r", encoding="utf-8", newline="").read()
    tsv_text = " ".join(words)
    if _non_whitespace(native_text) != _non_whitespace(tsv_text):
        raise ValueError("OCR native text and TSV text are inconsistent")

    average_confidence = round((sum(confidences) / len(confidences)) / 100.0, 4)
    return OCRResult(
        text=native_text,
        confidence=average_confidence,
        language=language,
        regions=tuple(regions),
    )


class LocalTesseractOCRProvider:
    def __init__(self, executable: str | None = None, timeout_seconds: int | None = None, language: str | None = None):
        self.executable = executable if executable is not None else settings.ocr_executable_path
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.ocr_timeout_seconds
        self.language = language if language is not None else settings.ocr_language

    @property
    def extractor_id(self) -> str:
        return f"local_tesseract:{Path(self.executable).name or 'unconfigured'}"

    def extract(self, content: bytes, mime_type: str) -> OCRResult:
        if not self.executable:
            raise ProviderError("ocr_not_configured", "No local OCR executable is configured.")

        with tempfile.TemporaryDirectory(prefix="ambient-ocr-") as raw_tmp_dir:
            tmp_dir = Path(raw_tmp_dir)
            input_path = tmp_dir / "input.png"
            output_base = tmp_dir / "output"
            working = prepare_ocr_image(content, input_path)

            try:
                completed = subprocess.run(
                    [self.executable, str(input_path), str(output_base), "-l", self.language, "--psm", str(settings.ocr_psm), "tsv", "txt"],
                    shell=False,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise ProviderError("ocr_timeout", "OCR command timed out.") from error
            except OSError as error:
                raise ProviderError("ocr_not_configured", "The configured OCR executable could not be run.") from error

            if completed.returncode != 0:
                raise ProviderError("ocr_nonzero_exit", "OCR command exited with an error.")

            tsv_path = output_base.with_suffix(".tsv")
            txt_path = output_base.with_suffix(".txt")
            if not tsv_path.exists() or not txt_path.exists():
                raise ProviderError("ocr_malformed_output", "OCR command produced no output.")
            # Bound the COMBINED output bytes before reading, so an oversized
            # output is reported as a size failure, not a parse failure.
            combined = tsv_path.stat().st_size + txt_path.stat().st_size
            if combined > settings.processing_max_output_bytes:
                raise ProviderError("ocr_output_too_large", "OCR output exceeded the size limit.")

            try:
                return working.normalized_regions(_parse(tsv_path, txt_path, self.language))
            except Exception as error:
                raise ProviderError("ocr_malformed_output", "OCR output could not be parsed.") from error
