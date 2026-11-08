import json
from dataclasses import FrozenInstanceError, replace

import pytest
from pydantic import ValidationError

from app.ai.gates.output_gate import validate_plan_selection
from app.ai.grounded_answering import (
    build_answer_plan,
    build_fact_sheet,
    build_query_frame,
    render_plan,
)
from app.ai.prompt_builder import build_plan_selection_prompt
from app.ai.providers.base import InferenceRequest
from app.ai.providers.ollama_provider import OllamaProvider
from app.retrieval.chunking import RetrievalChunk, sha256_text
from app.schemas.ai import AskCitation, AskRequest, AskResponse


def chunk(chunk_id="c1", text="아버지 검진은 오후 2시였다.", **changes):
    fields = dict(
        id=chunk_id,
        room_id="room-1",
        record_id="record-1",
        file_id="file-1",
        processing_job_id="job-1",
        source_attempt=1,
        source_result_sha256="a" * 64,
        ordinal=0,
        text=text,
        text_sha256=sha256_text(text),
        token_count=4,
        char_start=17,
        char_end=17 + len(text),
        source_locator_json='{"kind":"stt_segments","segments":[{"index":0}]}',
        extractor="AUTO_STT",
        extractor_id="stt-v1",
        review_status="unreviewed",
        user_confirmed=False,
        chunker_version="ws160-o32-v1",
    )
    fields.update(changes)
    return RetrievalChunk(**fields)


def test_query_frame_records_known_literals_but_does_not_authorize_answer():
    frame = build_query_frame("아빠 검진은 오후 2시, 2018년 3번이었어?")
    assert frame.normalized_query == "아빠 검진은 오후 2시, 2018년 3번이었어?"
    assert "아버지" in frame.entities
    assert "오후 2시" in frame.times
    assert "2018년" in frame.dates
    assert "3" in frame.numbers
    assert "검진" in frame.exact_terms
    assert frame.modality == "question"
    assert build_query_frame("무슨 일이야").predicate == "unknown"
    assert "route" not in frame.__dataclass_fields__


def test_query_frame_does_not_infer_negation_or_future_from_word_fragments():
    assert build_query_frame("안과 검진은 몇 시였어?").polarity == "unknown"
    assert build_query_frame("할머니 검진은 몇 시야?").tense == "unknown"


def test_fact_sheet_keeps_complete_chunk_and_exact_provenance():
    sheet = build_fact_sheet([chunk()])
    fact = sheet.facts[0]
    assert fact.quote == "아버지 검진은 오후 2시였다."
    assert (fact.quote_char_start, fact.quote_char_end) == (0, len(fact.quote))
    assert fact.source.chunk_id == "c1"
    assert (fact.source.chunk_char_start, fact.source.chunk_char_end) == (17, 17 + len(fact.quote))
    assert fact.source.source_locator_json == '{"kind":"stt_segments","segments":[{"index":0}]}'
    assert (fact.source.extractor, fact.source.review_status, fact.source.user_confirmed) == ("AUTO_STT", "unreviewed", False)


def test_fact_id_is_stable_only_for_same_source_version():
    source = chunk()
    original = build_fact_sheet([source]).facts[0].fact_id
    assert original == build_fact_sheet([source]).facts[0].fact_id
    for changed in (
        replace(source, source_attempt=2),
        replace(source, source_result_sha256="b" * 64),
        replace(source, char_start=18),
        replace(source, record_id="record-2"),
    ):
        assert build_fact_sheet([changed]).facts[0].fact_id != original


def test_fact_sheet_snapshots_immutable_source_fields():
    source = chunk()
    sheet = build_fact_sheet([source])
    with pytest.raises(FrozenInstanceError):
        sheet.facts[0].source.record_id = "other"
    with pytest.raises(FrozenInstanceError):
        sheet.facts = ()
    assert sheet.facts[0].source.record_id == "record-1"


@pytest.mark.parametrize("change", [
    {"room_id": ""},
    {"record_id": ""},
    {"char_start": -1},
    {"char_start": 8, "char_end": 7},
    {"text": ""},
])
def test_fact_sheet_rejects_invalid_identity_or_span(change):
    with pytest.raises(ValueError):
        build_fact_sheet([chunk(**change)])


def test_selection_rejects_prose_unknown_duplicate_and_non_json():
    plan = build_answer_plan(build_fact_sheet([chunk("c1", "2018년 여름")]), k=1)
    assert validate_plan_selection('{"claim_ids":["claim-1"]}', plan) == ("claim-1",)
    assert validate_plan_selection('{"claim_ids":["claim-9"]}', plan) is None
    assert validate_plan_selection('{"claim_ids":["claim-1","claim-1"]}', plan) is None
    assert validate_plan_selection('{"claim_ids":["claim-1"],"answer":"2019년"}', plan) is None
    assert validate_plan_selection("2019년입니다", plan) is None
    assert validate_plan_selection(None, plan) is None
    assert validate_plan_selection("[" * 1000 + "0" + "]" * 1000, plan) is None
    assert validate_plan_selection("x" * 8193, plan) is None


@pytest.mark.parametrize("raw", [
    '{"claim_ids":["claim-1"]}',
    '{"claim_ids":["claim-1","claim-2","claim-2"]}',
    '{"claim_ids":["claim-1",2]}',
    '{"claim_ids":"claim-1"}',
    '{"claim_ids":["claim-1","claim-2"],"claim_ids":[]}',
    '{}',
    '[]',
])
def test_selection_requires_exact_complete_permutation(raw):
    plan = build_answer_plan(build_fact_sheet([chunk(), chunk("c2", "어머니 산책은 3번이었다.")]), k=2)
    assert validate_plan_selection(raw, plan) is None
    assert validate_plan_selection('{"claim_ids":["claim-2","claim-1"]}', plan) == ("claim-2", "claim-1")


def test_render_plan_uses_only_selected_exact_quotes_and_extracted_citations():
    sheet = build_fact_sheet([chunk(), chunk("c2", "어머니 산책은 3번이었다.")])
    plan = build_answer_plan(sheet, k=2)
    answer, citations = render_plan(plan, ("claim-2", "claim-1"))
    assert answer == "기록에서 추출된 내용\n어머니 산책은 3번이었다.\n아버지 검진은 오후 2시였다."
    assert [citation["chunk_id"] for citation in citations] == ["c2", "c1"]
    assert citations[1] == {
        "fact_id": sheet.facts[0].fact_id,
        "chunk_id": "c1",
        "record_id": "record-1",
        "file_id": "file-1",
        "chunk_char_start": 17,
        "chunk_char_end": 17 + len(sheet.facts[0].quote),
        "source_locator": {"kind": "stt_segments", "segments": [{"index": 0}]},
        "extractor": "AUTO_STT",
        "extractor_id": "stt-v1",
        "review_status": "unreviewed",
        "user_confirmed": False,
        "evidence_status": "extracted",
    }
    assert "2019" not in answer
    assert "confirmed" not in answer


def test_render_plan_rejects_invalid_selection_or_locator():
    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    with pytest.raises(ValueError):
        render_plan(plan, ("claim-9",))
    with pytest.raises(ValueError):
        render_plan(plan, (["claim-1"],))
    broken = build_answer_plan(build_fact_sheet([chunk(source_locator_json="not json")]), k=1)
    with pytest.raises(ValueError):
        render_plan(broken, ("claim-1",))


@pytest.mark.parametrize("locator", [
    '{"kind":"full_text","storage_path":"private"}',
    '{"kind":"ocr_regions","indexes":[true]}',
    '{"kind":"stt_segments","segments":[{"index":0,"start_seconds":"private","end_seconds":2}]}',
    '{"kind":"stt_segments","segments":[{"index":0,"start_seconds":3,"end_seconds":2}]}',
    '{"kind":"stt_segments","segments":[{"index":0,"start_seconds":NaN,"end_seconds":2}]}',
])
def test_render_plan_rejects_unpublishable_locator_values(locator):
    plan = build_answer_plan(build_fact_sheet([chunk(source_locator_json=locator)]), k=1)
    with pytest.raises(ValueError, match="Invalid source locator"):
        render_plan(plan, ("claim-1",))


def test_render_plan_keeps_legitimate_missing_stt_timing():
    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    _answer, citations = render_plan(plan, ("claim-1",))
    assert citations[0]["source_locator"] == {"kind": "stt_segments", "segments": [{"index": 0}]}


def test_plan_prompt_compiles_into_existing_ollama_payload_without_network():
    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    package = build_plan_selection_prompt(model_id="exaone-45-33b-dgx", query="몇 시?", plan=plan)
    assert set(package) == {"model_profile", "instructions", "persona_markdown", "retrieved_evidence"}
    assert package["persona_markdown"] == ""
    assert "claim_ids" in package["instructions"]
    assert package["retrieved_evidence"] == [f"claim-1 | fact={plan.facts[0].fact_id} | quote=아버지 검진은 오후 2시였다."]
    payload = OllamaProvider(model_aliases={"exaone-45-33b-dgx": "local"}).build_payload(
        InferenceRequest(model_id="exaone-45-33b-dgx", user_query="몇 시?", prompt_package=package)
    )
    assert payload["model"] == "local"
    assert payload["messages"][0]["content"] == package["instructions"]
    assert "Persona Markdown:\n\n\nRetrieved Evidence:" in payload["messages"][1]["content"]
    assert package["retrieved_evidence"][0] in payload["messages"][1]["content"]


FENCE = chr(96) * 3


def test_selection_prompt_still_requests_bare_json_while_parser_accepts_one_fence():
    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    package = build_plan_selection_prompt(model_id="exaone-45-33b-dgx", query="몇 시?", plan=plan)
    instructions = package["instructions"]
    assert "claim_ids" in instructions
    assert "code fence" in instructions and "backtick" in instructions
    assert validate_plan_selection('{"claim_ids":["claim-1"]}', plan) == ("claim-1",)
    assert validate_plan_selection(FENCE + 'json\n{"claim_ids":["claim-1"]}\n' + FENCE, plan) == ("claim-1",)


@pytest.mark.parametrize("raw", [
    '{"claim_ids":["claim-1"]}',
    '  {"claim_ids":["claim-1"]}  ',
    FENCE + 'json\n{"claim_ids":["claim-1"]}\n' + FENCE,
    '  ' + FENCE + '\n{"claim_ids":["claim-1"]}\n' + FENCE + '  ',
])
def test_selection_accepts_bare_json_or_one_whole_fence(raw):
    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    assert validate_plan_selection(raw, plan) == ("claim-1",)


@pytest.mark.parametrize("raw", [
    'Answer: {"claim_ids":["claim-1"]}',
    '{"claim_ids":["claim-1"]} done',
    '{"claim_ids":["claim-1"]} {"claim_ids":["claim-1"]}',
    '{"claim_ids":[]}',
    '{"claim_ids":["claim-1"],"claim_ids":["claim-1"]}',
    FENCE + 'json\n{"claim_ids":["claim-1"]}\n' + FENCE + '\n' + FENCE + 'json\n{"claim_ids":["claim-1"]}\n' + FENCE,
    FENCE + 'python\n{"claim_ids":["claim-1"]}\n' + FENCE,
    FENCE + 'json\n{"claim_ids":["claim-1"]}',
    FENCE + 'json\n' + FENCE + 'json\n{"claim_ids":["claim-1"]}\n' + FENCE + '\n' + FENCE,
])
def test_selection_rejects_prose_multiple_json_and_malformed_fences(raw):
    plan = build_answer_plan(build_fact_sheet([chunk()]), k=1)
    assert validate_plan_selection(raw, plan) is None


def test_ask_dtos_validate_inputs_and_public_citation_shape():
    request = AskRequest(query="몇 시?", model_id="exaone-45-33b-dgx", arm="hybrid_rrf", k=3)
    assert request.k == 3
    for bad in ({"query": ""}, {"query": "  \n  "}, {"query": "x" * 4097},
                {"model_id": "m" * 129}, {"arm": "wrong"}, {"k": 0}, {"k": 51}):
        with pytest.raises(ValidationError):
            AskRequest.model_validate({**request.model_dump(), **bad})
    answer, citations = render_plan(build_answer_plan(build_fact_sheet([chunk()]), k=1), ("claim-1",))
    response = AskResponse(gate_route="ANSWER", answer=answer, citations=[AskCitation.model_validate(citations[0])], gate_action="literal_render", provider_mode="fallback")
    public = json.dumps(response.model_dump())
    for hidden in ("source_result_sha256", "text_sha256", "processing_job_id", "score", "prompt", "raw_output"):
        assert hidden not in public
