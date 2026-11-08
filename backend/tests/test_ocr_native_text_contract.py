"""Native Tesseract text-contract tests.

These mock `subprocess.run` to write a single-invocation TSV (fragmented word
rows) and a native TXT (correctly spaced) and assert the provider keeps the
native TXT text while retaining TSV confidence/regions/geometry, and fails
closed on inconsistent, missing, empty, invalid-UTF8 or oversized output. No
real Tesseract/Whisper executable is invoked.
"""
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
from app.retrieval.chunking import ChunkSource, chunk_processing_result, sha256_text

ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478da6360606060000000050001a5f645400000000049454e44ae426082"
)

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"

# Fragmented TSV word rows — the provider must NOT reconstruct these as the text.
# A TSV header is required so the CSV parser reads real column names (without it
# the first data row becomes the header and no `text`/`conf` columns exist).
_CAFE_TSV = (
    TSV_HEADER
    + "5\t1\t1\t1\t1\t1\t10\t20\t30\t15\t90.0\t카\n"
    + "5\t1\t1\t1\t1\t2\t45\t20\t30\t15\t90.0\t페\n"
    + "5\t1\t1\t1\t1\t3\t80\t20\t30\t15\t90.0\t에서\n"
    + "5\t1\t1\t1\t1\t4\t115\t20\t30\t15\t90.0\t친\n"
    + "5\t1\t1\t1\t1\t5\t150\t20\t30\t15\t90.0\t구\n"
    + "5\t1\t1\t1\t1\t6\t185\t20\t30\t15\t90.0\t를\n"
    + "5\t1\t1\t1\t1\t7\t220\t20\t30\t15\t90.0\t만났다.\n"
)
# Native TXT for the same invocation: correctly spaced, same non-whitespace
# character sequence as the TSV-joined words.
_CAFE_NATIVE_TXT = "카페에서 친구를 만났다.\n"
_TSV_JOINED = "카 페 에서 친 구 를 만났다."


def _run(tsv: str, txt: str | None = "written", txt_bytes: bytes | None = None):
    def side_effect(argv, **kwargs):
        base = Path(argv[2])
        base.with_suffix(".tsv").write_text(tsv, encoding="utf-8")
        if txt is not None:
            base.with_suffix(".txt").write_text(txt, encoding="utf-8")
        if txt_bytes is not None:
            base.with_suffix(".txt").write_bytes(txt_bytes)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    return side_effect


def _provider():
    return LocalTesseractOCRProvider(executable="tesseract", timeout_seconds=5, language="kor+eng")


# --------------------------------------------------------------------------- #
def test_korean_native_word_boundaries_survive_and_tsv_regions_unchanged():
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run", side_effect=_run(_CAFE_TSV, _CAFE_NATIVE_TXT)):
        result = provider.extract(ONE_PIXEL_PNG, "image/png")

    # The extracted text is the native TXT (correct spacing), not the TSV join.
    assert result.text == _CAFE_NATIVE_TXT
    assert result.text != _TSV_JOINED
    assert result.text == "카페에서 친구를 만났다.\n"
    # TSV regions (confidence/geometry) are retained and unchanged.
    assert len(result.regions) == 7
    assert result.regions[0].text == "카"
    assert result.regions[0].left == 10
    assert result.regions[6].text == "만났다."
    assert 0.0 <= result.confidence <= 1.0


def test_genuine_inter_word_newline_is_retained():
    tsv = TSV_HEADER + "5\t1\t1\t1\t1\t1\t10\t20\t30\t15\t90.0\t안녕\n" \
                        + "5\t1\t1\t1\t1\t2\t45\t20\t30\t15\t90.0\t하세요\n"
    # native TXT splits the two words across lines; TSV joins them with a space.
    native_txt = "안녕\n하세요\n"
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run", side_effect=_run(tsv, native_txt)):
        result = provider.extract(ONE_PIXEL_PNG, "image/png")

    assert result.text == native_txt
    assert "\n" in result.text
    assert len(result.regions) == 2
    assert result.regions[0].text == "안녕"


def test_missing_txt_fails_closed():
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run", side_effect=_run(_CAFE_TSV, txt=None)):
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_malformed_output"


def test_empty_txt_with_words_fails_closed():
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run", side_effect=_run(_CAFE_TSV, "")):
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_malformed_output"


def test_mismatched_txt_fails_closed():
    # same characters but one extra -> non-whitespace sequences differ.
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run", side_effect=_run(_CAFE_TSV, "카페에서 친구를 만났다.\n요\n")):
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_malformed_output"


def test_invalid_utf8_txt_fails_closed():
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run",
               side_effect=_run(_CAFE_TSV, txt=None, txt_bytes=b"\xff\xfe\x00 caf\xe3")):
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_malformed_output"


def test_combined_output_bound_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "processing_max_output_bytes", 16)
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run",
               side_effect=_run(_CAFE_TSV, _CAFE_NATIVE_TXT)):
        with pytest.raises(ProviderError) as exc_info:
            provider.extract(ONE_PIXEL_PNG, "image/png")
    assert exc_info.value.code == "ocr_output_too_large"


def test_single_invocation_requests_tsv_and_txt():
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run",
               side_effect=_run(_CAFE_TSV, _CAFE_NATIVE_TXT)) as mock_run:
        provider.extract(ONE_PIXEL_PNG, "image/png")
    argv = mock_run.call_args[0][0]
    assert argv[0] == "tesseract"
    assert argv[-2:] == ["tsv", "txt"]


def test_chunk_contract_consistent_with_split_tsv_tokens():
    """chunk_processing_result (unchanged) stays raw-span/source-hash/locator
    consistent when the raw text is native and the TSV tokens are split."""
    result = {
        "text": _CAFE_NATIVE_TXT,
        "regions": [
            {"text": "카"}, {"text": "페"}, {"text": "에서"}, {"text": "친"},
            {"text": "구"}, {"text": "를"}, {"text": "만났다."},
        ],
        "extractor": "AUTO_OCR",
        "extractor_id": "local_tesseract:tesseract",
        "review_status": "unreviewed",
        "user_confirmed": False,
    }
    source = ChunkSource(
        room_id="room-1", record_id="record-1", file_id="file-1",
        processing_job_id="job-1", source_attempt=1, result=result,
    )
    chunks = chunk_processing_result(source)
    assert len(chunks) == 1
    chunk = chunks[0]

    # raw span is an offset into the raw (native) text, not the TSV-joined text.
    assert 0 <= chunk.char_start < chunk.char_end <= len(_CAFE_NATIVE_TXT)
    # text is the contract-normalized native slice; hash is internally consistent.
    assert chunk.text_sha256 == sha256_text(chunk.text)
    assert chunk.text  # non-empty
    # source-result hash is the canonical hash of the frozen result.
    assert chunk.source_result_sha256
    # locator is valid JSON and references the split TSV region indexes.
    locator = json.loads(chunk.source_locator_json)
    assert isinstance(locator, dict)
    assert locator["kind"] in ("ocr_regions", "full_text")


# --------------------------------------------------------------------------- #
# Native TXT line-ending fidelity: the provider must NOT apply universal-newline
# translation. read_text's default newline=None would collapse CRLF and lone CR
# to LF; with newline="" the native bytes are preserved verbatim. The fixtures
# write native TXT via exact bytes (txt_bytes) so the on-disk content is the
# CRLF/CR form; the TSV word is the same characters, so the non-whitespace
# consistency check passes and only the line endings differ.

_CRL = "하" + "세" + "요"
_CRLF = "안녕" + "\r\n" + _CRL + "\r\n"
_CR = "안녕" + "\r" + _CRL + "\r"


def _two_word_tsv():
    # Two TSV word rows whose text equals the native non-whitespace sequence
    # (안녕 / 하세 요), so only the line endings differ between TXT and TSV.
    return (TSV_HEADER
            + "5\t1\t1\t1\t1\t1\t10\t20\t30\t15\t90.0\t안녕\n"
            + "5\t1\t1\t1\t1\t2\t45\t20\t30\t15\t90.0\t" + _CRL + "\n")


def test_crlf_native_txt_is_preserved_exactly():
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run",
               side_effect=_run(_two_word_tsv(), txt=None, txt_bytes=_CRLF.encode("utf-8"))):
        result = provider.extract(ONE_PIXEL_PNG, "image/png")
    # Exact CRLF preserved verbatim (read_text would collapse "\r\n" to "\n").
    assert result.text == _CRLF
    assert result.text.count("\r\n") == 2
    # Removing the CRLF pairs leaves a clean line with no bare LF behind.
    assert "\n" not in result.text.replace("\r\n", "")
    assert len(result.regions) == 2
    assert result.regions[0].text == "안녕"
    assert result.regions[1].text == _CRL


def test_cr_native_txt_is_preserved_exactly():
    # Lone CR must survive verbatim (read_text would collapse "\r" to "\n").
    provider = _provider()
    with patch("app.processing.local_ocr.subprocess.run",
               side_effect=_run(_two_word_tsv(), txt=None, txt_bytes=_CR.encode("utf-8"))):
        result = provider.extract(ONE_PIXEL_PNG, "image/png")
    assert result.text == _CR
    assert result.text.count("\r") == 2
    assert "\n" not in result.text
    assert len(result.regions) == 2
    assert result.regions[1].text == _CRL


@pytest.mark.parametrize('tokens', [
    ['"hello', 'world'],
    ['"hello"', '"world"'],
    ['"', '안녕', '"'],
    ['say"hello', 'path\\name'],
])
def test_tesseract_tsv_quotes_are_literal_text(tokens: list[str]) -> None:
    tsv = TSV_HEADER + ''.join(
        f'5\t1\t1\t1\t1\t{i}\t10\t20\t30\t15\t20.0\t{token}\n'
        for i, token in enumerate(tokens, 1)
    )
    native = ' '.join(tokens) + '\n'
    with patch('app.processing.local_ocr.subprocess.run', side_effect=_run(tsv, native)):
        result = _provider().extract(ONE_PIXEL_PNG, 'image/png')
    assert result.text == native
    assert [r.text for r in result.regions] == tokens
    assert all(r.confidence == 0.2 for r in result.regions)
    assert result.confidence == 0.2
    with (
        patch('app.processing.local_ocr.subprocess.run', side_effect=_run(tsv, native + 'unexpected')),
        pytest.raises(ProviderError) as error,
    ):
        _ = _provider().extract(ONE_PIXEL_PNG, 'image/png')
    assert error.value.code == 'ocr_malformed_output'


@pytest.mark.parametrize('confidence', [0, 1, 49, 99])
def test_low_confidence_is_prediction_not_approval(confidence: int) -> None:
    tsv = TSV_HEADER + f'5\t1\t1\t1\t1\t1\t10\t20\t30\t15\t{confidence}\tword\n'
    with patch('app.processing.local_ocr.subprocess.run', side_effect=_run(tsv, 'word\n')):
        result = _provider().extract(ONE_PIXEL_PNG, 'image/png')
    assert result.text == 'word\n'
    assert result.regions[0].confidence == confidence / 100
    assert result.confidence == confidence / 100
