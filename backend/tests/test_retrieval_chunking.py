import json

import pytest
from pydantic import ValidationError

from app.retrieval.chunking import (
    ChunkSource,
    _structured_spans,
    _tokens,
    chunk_processing_result,
)
from app.schemas.retrieval import (
    RetrievalHitResponse,
    RetrievalIndexRequest,
    RetrievalIndexResponse,
    RetrievalSearchRequest,
    RetrievalSearchResponse,
)


def source(result: dict, *, attempt: int = 1) -> ChunkSource:
    return ChunkSource(
        room_id="room-1",
        record_id="record-1",
        file_id="file-1",
        processing_job_id="job-1",
        source_attempt=attempt,
        result=result,
    )


def test_normalizes_nfkc_whitespace_and_preserves_korean() -> None:
    chunks = chunk_processing_result(source({"text": "  ＡＢＣ\n\t가족   기록  "}))

    assert [chunk.text for chunk in chunks] == ["ABC 가족 기록"]
    assert (chunks[0].char_start, chunks[0].char_end) == (2, 14)


def test_empty_input_has_no_chunks() -> None:
    assert chunk_processing_result(source({"text": " \n\t"})) == ()


def test_uses_exact_160_token_windows_with_32_token_overlap() -> None:
    text = " ".join(f"t{i}" for i in range(289))
    chunks = chunk_processing_result(source({"text": text}))

    assert [chunk.token_count for chunk in chunks] == [160, 160, 33]
    assert chunks[0].text.split()[0:2] == ["t0", "t1"]
    assert chunks[1].text.split()[0:2] == ["t128", "t129"]
    assert chunks[2].text.split()[0] == "t256"


def test_chunk_ids_are_stable_and_change_with_source_attempt() -> None:
    result = {"text": "같은 원문"}
    first = chunk_processing_result(source(result))
    retried = chunk_processing_result(source(result))
    changed = chunk_processing_result(source(result, attempt=2))

    assert first[0].id == retried[0].id
    assert first[0].id != changed[0].id
    assert first[0].source_result_sha256 == changed[0].source_result_sha256


def test_ocr_regions_are_aligned_and_unreviewed_provenance_is_retained() -> None:
    result = {
        "text": "첫 문장 둘째 문장",
        "regions": [{"text": "첫 문장"}, {"text": "둘째 문장"}],
        "extractor": "AUTO_OCR",
        "extractor_id": "tesseract:kor",
        "review_status": "unreviewed",
        "user_confirmed": False,
    }
    chunk = chunk_processing_result(source(result))[0]

    assert json.loads(chunk.source_locator_json) == {
        "indexes": [0, 1],
        "kind": "ocr_regions",
    }
    assert chunk.extractor == "AUTO_OCR"
    assert chunk.extractor_id == "tesseract:kor"
    assert chunk.review_status == "unreviewed"
    assert chunk.user_confirmed is False


def test_stt_segments_keep_time_locators() -> None:
    result = {
        "text": "안녕 가족",
        "segments": [
            {"text": "안녕", "start_seconds": 0.0, "end_seconds": 0.8},
            {"text": "가족", "start_seconds": 0.8, "end_seconds": 1.4},
        ],
        "extractor": "AUTO_STT",
    }
    chunk = chunk_processing_result(source(result))[0]

    assert json.loads(chunk.source_locator_json) == {
        "kind": "stt_segments",
        "segments": [
            {"end_seconds": 0.8, "index": 0, "start_seconds": 0.0},
            {"end_seconds": 1.4, "index": 1, "start_seconds": 0.8},
        ],
    }


def test_unmatchable_structured_source_falls_back_to_full_text() -> None:
    result = {"text": "실제 원문", "regions": [{"text": "없는 문장"}]}

    chunk = chunk_processing_result(source(result))[0]

    assert json.loads(chunk.source_locator_json) == {"kind": "full_text"}


def test_repeated_source_uses_earliest_greedy_match() -> None:
    result = {"text": "반복 반복", "regions": [{"text": "반복"}]}

    chunk = chunk_processing_result(source(result))[0]

    assert json.loads(chunk.source_locator_json) == {"indexes": [0], "kind": "ocr_regions"}


def test_repeated_stt_entries_align_greedily_as_one_complete_sequence() -> None:
    result = {
        "text": "안녕 안녕",
        "segments": [
            {"text": "안녕", "start_seconds": 0.0, "end_seconds": 0.5},
            {"text": "안녕", "start_seconds": 0.5, "end_seconds": 1.0},
        ],
    }

    chunk = chunk_processing_result(source(result))[0]

    assert [item["index"] for item in json.loads(chunk.source_locator_json)["segments"]] == [0, 1]


def test_three_occurrences_with_two_entries_choose_first_two() -> None:
    result = {"text": "안녕 안녕 안녕", "regions": [{"text": "안녕"}, {"text": "안녕"}]}

    structured = _structured_spans(result, _tokens(result["text"]))

    assert structured == ("ocr_regions", [(0, 2, 0), (3, 5, 1)])


def test_more_than_one_thousand_structured_entries_do_not_recurse() -> None:
    words = [f"단어{i}" for i in range(1100)]
    result = {"text": " ".join(words), "regions": [{"text": word} for word in words]}

    chunk = chunk_processing_result(source(result))[0]

    assert json.loads(chunk.source_locator_json)["indexes"] == list(range(160))


def test_chunk_source_snapshots_nested_caller_input() -> None:
    result = {"text": "원본", "regions": [{"text": "원본"}]}
    frozen_source = source(result)
    result["text"] = "변조"
    result["regions"][0]["text"] = "변조"

    chunk = chunk_processing_result(frozen_source)[0]

    assert chunk.text == "원본"
    assert json.loads(chunk.source_locator_json) == {"indexes": [0], "kind": "ocr_regions"}


def test_search_schema_locks_arms_and_k_range() -> None:
    assert RetrievalSearchRequest(query="기억", arm="bm25", k=3).k == 3
    with pytest.raises(ValidationError):
        RetrievalSearchRequest(query="기억", arm="unknown", k=3)
    with pytest.raises(ValidationError):
        RetrievalSearchRequest(query="기억", arm="bm25", k=0)


def test_public_response_and_index_schemas_construct() -> None:
    hit = RetrievalHitResponse(
        chunk_id="chunk-1",
        record_id="record-1",
        file_id="file-1",
        text="기억",
        source_locator={"kind": "full_text"},
        rank=1,
        scores={"bm25": 1.5},
    )
    response = RetrievalSearchResponse(arm="bm25", hits=[hit])
    request = RetrievalIndexRequest(processing_job_id="job-1")
    indexed = RetrievalIndexResponse(
        source_attempt=1,
        source_result_sha256="a" * 64,
        chunk_count=1,
        embedding_count=1,
        chunker_version="ws160-o32-v1",
        model_key="bge-m3@revision",
    )

    assert response.hits[0].chunk_id == "chunk-1"
    assert request.processing_job_id == "job-1"
    assert indexed.model_key == "bge-m3@revision"
