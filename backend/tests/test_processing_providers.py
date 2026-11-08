"""Local OCR/STT provider contract tests (Milestone 1 Task 5). `subprocess.run` is
always mocked here — these never invoke a real Tesseract/Whisper executable."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.processing.contracts import ProviderError
from app.processing.local_ocr import LocalTesseractOCRProvider
from app.processing.local_stt import LocalWhisperSTTProvider
from app.services.media_loader import media_family

ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478da6360606060000000050001a5f645400000000049454e44ae426082"
)
FAKE_WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt "

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
TSV_ROWS = (
    "5\t1\t1\t1\t1\t1\t10\t20\t30\t15\t95.5\t안녕\n"
    "5\t1\t1\t1\t1\t2\t45\t20\t30\t15\t88.0\t하세요\n"
)

# Native Tesseract TXT for the TSV rows above: the engine's own text with its own
# spacing (no invented inter-word separators). Its non-whitespace sequence equals
# the TSV-joined text, so the provider keeps this TXT verbatim.
NATIVE_TXT = "안녕하세요\n"

def _write_ocr_success(argv, **kwargs):
    Path(argv[2]).with_suffix(".tsv").write_text(TSV_HEADER + TSV_ROWS, encoding="utf-8")
    Path(argv[2]).with_suffix(".txt").write_text(NATIVE_TXT, encoding="utf-8")
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def _write_ocr_empty(argv, **kwargs):
    Path(argv[2]).with_suffix(".tsv").write_text(TSV_HEADER, encoding="utf-8")
    Path(argv[2]).with_suffix(".txt").write_text("", encoding="utf-8")
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def _write_stt_success(argv, **kwargs):
    input_path = Path(argv[1])
    input_path.with_suffix(".json").write_text(
        json.dumps({
            "text": "안녕하세요",
            "language": "ko",
            "segments": [{"text": "안녕하세요", "start": 0.0, "end": 1.2}],
        }),
        encoding="utf-8",
    )
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def _write_stt_empty(argv, **kwargs):
    input_path = Path(argv[1])
    input_path.with_suffix(".json").write_text(json.dumps({"text": "", "segments": []}), encoding="utf-8")
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


# ---------------------------------------------------------------- OCR ----


def test_ocr_provider_uses_argument_array_with_shell_false_and_maps_tsv():
    provider = LocalTesseractOCRProvider(executable="tesseract", timeout_seconds=5, language="kor+eng")
    with patch("app.processing.local_ocr.subprocess.run") as mock_run:
        mock_run.side_effect = _write_ocr_success
        result = provider.extract(ONE_PIXEL_PNG, "image/png")

    argv, kwargs = mock_run.call_args
    assert kwargs["shell"] is False
    assert isinstance(argv[0], list)
    assert argv[0][0] == "tesseract"
    assert kwargs["timeout"] == 5
    assert argv[0][-2:] == ["tsv", "txt"]

    # text comes from native TXT (preserved), not the TSV-joined "안녕 하세요"
    assert result.text == NATIVE_TXT
    assert result.language == "kor+eng"
    assert 0.0 <= result.confidence <= 1.0
    assert len(result.regions) == 2
    assert result.regions[0].text == "안녕"
    assert result.regions[0].left == 10


def test_ocr_provider_enforces_timeout():
    provider = LocalTesseractOCRProvider(executable="tesseract", timeout_seconds=1)
    with patch("app.processing.local_ocr.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tesseract", timeout=1)
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_timeout"


def test_ocr_provider_nonzero_exit_becomes_typed_error():
    provider = LocalTesseractOCRProvider(executable="tesseract")
    with patch("app.processing.local_ocr.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_nonzero_exit"


def test_ocr_provider_malformed_output_becomes_typed_error():
    provider = LocalTesseractOCRProvider(executable="tesseract")
    with patch("app.processing.local_ocr.subprocess.run") as mock_run:
        mock_run.side_effect = _write_ocr_empty
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_malformed_output"


def test_ocr_provider_not_configured_without_executable():
    provider = LocalTesseractOCRProvider(executable="")
    with pytest.raises(ProviderError) as exc_info:
        provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_not_configured"


# ---------------------------------------------------------------- STT ----


def test_stt_provider_uses_argument_array_with_shell_false_and_maps_json():
    provider = LocalWhisperSTTProvider(executable="whisper", timeout_seconds=10)
    with patch("app.processing.local_stt.subprocess.run") as mock_run:
        mock_run.side_effect = _write_stt_success
        result = provider.transcribe(FAKE_WAV_BYTES, "audio/wav", language="ko")

    argv, kwargs = mock_run.call_args
    assert kwargs["shell"] is False
    assert isinstance(argv[0], list)
    assert argv[0][0] == "whisper"
    assert kwargs["timeout"] == 10

    assert result.text == "안녕하세요"
    assert result.language == "ko"
    assert len(result.segments) == 1
    assert result.segments[0].start_seconds == 0.0
    assert result.segments[0].end_seconds == 1.2


def test_stt_provider_enforces_timeout():
    provider = LocalWhisperSTTProvider(executable="whisper", timeout_seconds=1)
    with patch("app.processing.local_stt.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="whisper", timeout=1)
        with pytest.raises(ProviderError) as exc_info:
            provider.transcribe(FAKE_WAV_BYTES, "audio/wav")
    assert exc_info.value.code == "stt_timeout"


def test_stt_provider_nonzero_exit_becomes_typed_error():
    provider = LocalWhisperSTTProvider(executable="whisper")
    with patch("app.processing.local_stt.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")
        with pytest.raises(ProviderError) as exc_info:
            provider.transcribe(FAKE_WAV_BYTES, "audio/wav")
    assert exc_info.value.code == "stt_nonzero_exit"


def test_stt_provider_malformed_output_becomes_typed_error():
    provider = LocalWhisperSTTProvider(executable="whisper")
    with patch("app.processing.local_stt.subprocess.run") as mock_run:
        mock_run.side_effect = _write_stt_empty
        with pytest.raises(ProviderError) as exc_info:
            provider.transcribe(FAKE_WAV_BYTES, "audio/wav")
    assert exc_info.value.code == "stt_malformed_output"


def test_stt_provider_not_configured_without_executable():
    provider = LocalWhisperSTTProvider(executable="")
    with pytest.raises(ProviderError) as exc_info:
        provider.transcribe(FAKE_WAV_BYTES, "audio/wav")
    assert exc_info.value.code == "stt_not_configured"


# ------------------------- R8: bounds, language, provenance, strict parsing ----


def test_ocr_argv_carries_configured_language_and_extractor_id():
    provider = LocalTesseractOCRProvider(executable="tesseract", timeout_seconds=5, language="kor+eng")
    with patch("app.processing.local_ocr.subprocess.run") as mock_run:
        mock_run.side_effect = _write_ocr_success
        provider.extract(ONE_PIXEL_PNG, "image/png")
    argv = mock_run.call_args[0][0]
    assert argv[3:] == ["-l", "kor+eng", "--psm", "6", "tsv", "txt"]
    assert provider.extractor_id == "local_tesseract:tesseract"


@pytest.mark.parametrize(
    "conf, left",
    [("nan", "10"), ("inf", "10"), ("150", "10"), ("95.5", "-4"), ("95.5", "nan"), ("95.5", "abc")],
)
def test_ocr_parser_rejects_non_finite_or_out_of_range_values(conf, left):
    def write_bad(argv, **kwargs):
        Path(argv[2]).with_suffix(".tsv").write_text(
            TSV_HEADER + f"5\t1\t1\t1\t1\t1\t{left}\t20\t30\t15\t{conf}\t안녕\n", encoding="utf-8"
        )
        # A valid TXT is written alongside so the failure is the malformed TSV,
        # not a missing TXT (the malformed TSV is rejected before TXT is read).
        Path(argv[2]).with_suffix(".txt").write_text("안녕\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    provider = LocalTesseractOCRProvider(executable="tesseract", timeout_seconds=5, language="kor+eng")
    with patch("app.processing.local_ocr.subprocess.run", side_effect=write_bad):
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_malformed_output"


def test_ocr_output_size_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "processing_max_output_bytes", 64)

    def write_huge(argv, **kwargs):
        Path(argv[2]).with_suffix(".tsv").write_text(TSV_HEADER + TSV_ROWS * 10, encoding="utf-8")
        Path(argv[2]).with_suffix(".txt").write_text(NATIVE_TXT * 10, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    provider = LocalTesseractOCRProvider(executable="tesseract", timeout_seconds=5, language="kor+eng")
    with patch("app.processing.local_ocr.subprocess.run", side_effect=write_huge):
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_output_too_large"


def test_stt_argv_carries_configured_language_and_extractor_id(monkeypatch):
    monkeypatch.setattr(settings, "stt_language", "ko")
    provider = LocalWhisperSTTProvider(executable="whisper", timeout_seconds=5)
    with patch("app.processing.local_stt.subprocess.run") as mock_run:
        mock_run.side_effect = _write_stt_success
        provider.transcribe(FAKE_WAV_BYTES, "audio/wav")
    argv = mock_run.call_args[0][0]
    assert argv[argv.index("--language") + 1] == "ko"
    assert provider.extractor_id == "local_whisper:whisper"

    with patch("app.processing.local_stt.subprocess.run") as mock_run:
        mock_run.side_effect = _write_stt_success
        provider.transcribe(FAKE_WAV_BYTES, "audio/wav", language="en")
    assert mock_run.call_args[0][0][mock_run.call_args[0][0].index("--language") + 1] == "en"


@pytest.mark.parametrize(
    "payload_text",
    [
        '{"text": "hi", "segments": [{"text": "hi", "start": NaN, "end": 1.0}]}',
        '{"text": "hi", "segments": [{"text": "hi", "start": 0.0, "end": Infinity}]}',
        '{"text": "hi", "segments": [{"text": "hi", "start": 2.0, "end": 1.0}]}',
        '{"text": "hi", "segments": [{"text": "a", "start": 0.0, "end": 1.0}, {"text": "b", "start": 0.5, "end": 2.0}]}',
        '{"text": "hi", "segments": [{"text": "hi", "start": -1.0, "end": 1.0}]}',
        '{"text": "hi", "segments": [{"text": "hi", "start": true, "end": 1.0}]}',
        '{"text": "hi", "segments": [{"text": "hi", "start": "0", "end": 1.0}]}',
        '{"text": "hi", "segments": [{"text": 5, "start": 0.0, "end": 1.0}]}',
        '{"text": "hi", "segments": ["not a segment"]}',
        '{"text": ["not", "a", "string"], "segments": []}',
        '["not", "an", "object"]',
    ],
)
def test_stt_parser_rejects_invalid_shapes(payload_text):
    def write_bad(argv, **kwargs):
        Path(argv[1]).with_suffix(".json").write_text(payload_text, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    provider = LocalWhisperSTTProvider(executable="whisper", timeout_seconds=5)
    with patch("app.processing.local_stt.subprocess.run", side_effect=write_bad):
        with pytest.raises(ProviderError) as exc_info:
            provider.transcribe(FAKE_WAV_BYTES, "audio/wav")
    assert exc_info.value.code == "stt_malformed_output"


def test_stt_output_size_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "processing_max_output_bytes", 16)
    provider = LocalWhisperSTTProvider(executable="whisper", timeout_seconds=5)
    with patch("app.processing.local_stt.subprocess.run", side_effect=_write_stt_success):
        with pytest.raises(ProviderError) as exc_info:
            provider.transcribe(FAKE_WAV_BYTES, "audio/wav")
    assert exc_info.value.code == "stt_output_too_large"


def test_media_family_sniffs_magic_numbers():
    assert media_family(ONE_PIXEL_PNG) == {"image"}
    assert media_family(FAKE_WAV_BYTES) == {"audio"}
    assert media_family(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == {"image"}
    assert media_family(b"ID3\x03" + b"\x00" * 8) == {"audio"}
    assert media_family(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4) == {"audio", "video"}
    assert media_family(b"fake-bytes") is None
    assert media_family(b"") is None
