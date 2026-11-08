"""Synthetic M4 contract proofs, not real model or canonical dataset evaluation."""
import io
import wave
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.processing.contracts import OCRResult, STTResult
from app.processing.multimodal import (
    DecodedMedia,
    SyntheticImageOCR,
    SyntheticSpeechProvider,
    SyntheticVisualProvider,
    eligible,
    extract,
    review,
)
from app.retrieval.chunking import canonical_json, sha256_text
from app.schemas.multimodal import MediaReviewRequest, VisualPrediction
from app.services.multimodal_jobs import review_digest
from app.services.processing_jobs import processing_result_is_evidence
from PIL import Image


def png():
    data = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(data, format="PNG")
    return data.getvalue()


def wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 16000)
    return data.getvalue()


def request(result, action="approve", **kw):
    return MediaReviewRequest(action=action, source_attempt=0,
        source_result_sha256=sha256_text(canonical_json(result)), review_version=result["m4"]["review_version"], **kw)


def test_image_empty_success_and_approval():
    result = extract(png(), "image/png", ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider())
    assert result["m4"]["media_metadata"]["ocr_outcome"] == "OCR_EMPTY_SUCCESS"
    assert not eligible(result) and result["text"] == ""
    accepted = review(result, request(result), 0)
    assert eligible(accepted) and accepted["text"] == "Synthetic visual description"
    assert accepted["m4"]["predicted"] == result["m4"]["predicted"]
    assert accepted["m4"]["gold"] is None


def test_ocr_text_is_separate_from_visual_and_correctable():
    class OCR:
        extractor_id = "synthetic-text"
        def extract(self, content, mime):
            return OCRResult("misspelt", 1, "ko")
    result = extract(png(), "image/png", ocr=OCR(), visual=SyntheticVisualProvider())
    assert result["m4"]["media_metadata"]["ocr_outcome"] == "OCR_TEXT_FOUND"
    correction = dict(result["m4"]["predicted"], ocr_text="corrected")
    accepted = review(result, request(result, "correct", approved=correction), 0)
    assert accepted["m4"]["predicted"]["ocr_text"] == "misspelt"
    assert "corrected" in accepted["text"] and "misspelt" not in accepted["text"]
    rejected = review(accepted, request(accepted, "reject"), 0)
    assert rejected["text"] == "" and not eligible(rejected)


def test_audio_segments_and_distinct_clocks():
    result = extract(wav(), "audio/wav", speech=SyntheticSpeechProvider())
    assert result["m4"]["duration_ms"] == 1000
    assert result["m4"]["predicted"]["segments"][0]["end_ms"] == 500
    accepted = review(result, request(result), 0)
    assert accepted["text"] == "Synthetic speech"
    assert result["text"] == ""


def test_stale_review_and_anchor_rewrite_rejected():
    result = extract(wav(), "audio/wav", speech=SyntheticSpeechProvider())
    payload = request(result)
    with pytest.raises(ValueError, match="STALE_SOURCE"):
        review(result, payload, 1)
    correction = dict(result["m4"]["predicted"], segments=[])
    with pytest.raises(ValueError, match="anchors"):
        review(result, request(result, "correct", approved=correction), 0)


def test_synthetic_video_timeline():
    class Decoder:
        extractor_id = "synthetic-video-decoder"
        def decode(self, content, mime):
            return DecodedMedia(1000, wav(), ((0, png()), (500, png())))
    result = extract(b"synthetic-video", "video/mp4", decoder=Decoder(),
        speech=SyntheticSpeechProvider(), ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider())
    media = result["m4"]
    assert media["duration_ms"] == 1000 and media["audio_track_sha256"]
    assert [f["timestamp_ms"] for f in media["predicted"]["frames"]] == [0, 500]
    assert all(f["ocr_outcome"] == "OCR_EMPTY_SUCCESS" for f in media["predicted"]["frames"])
    assert eligible(review(result, request(result), 0))


def test_no_unreviewed_stt_evidence_and_rejection():
    result = extract(wav(), "audio/wav", speech=SyntheticSpeechProvider())
    job = SimpleNamespace(status="SUCCEEDED", processor="stt", attempt=0,
        result_json=canonical_json(result), review_json=None)
    assert not processing_result_is_evidence(job)
    result = review(result, request(result), 0)
    job.result_json = canonical_json(result)
    job.review_json = review_digest(result, 0, "approve")
    assert processing_result_is_evidence(job)
    result = review(result, request(result, "reject"), 0)
    job.result_json = canonical_json(result)
    job.review_json = review_digest(result, 0, "reject")
    assert not processing_result_is_evidence(job)


def test_visual_provider_cannot_supply_known_identity():
    with pytest.raises(ValueError):
        VisualPrediction(visible_member_ids=[str(uuid4())])


def test_silent_audio_is_valid():
    class Silent:
        extractor_id = "synthetic-silent"
        def transcribe(self, content, mime, language):
            return STTResult("", "ko", ())
    result = extract(wav(), "audio/wav", speech=Silent())
    assert result["m4"]["stages"]["speech"] == "VALID_MEDIA_NO_SPEECH"
    assert review(result, request(result), 0)["text"] == ""


def test_recorded_event_clocks_and_trusted_speaker_survive_review():
    result = extract(wav(), "audio/wav", speech=SyntheticSpeechProvider())
    time = lambda year: {"precision": "YEAR", "value": str(year), "start": None, "end": None,
        "granularity": "YEAR", "timezone": "Asia/Seoul", "original_text": str(year), "qualification": None}
    speaker = str(uuid4())
    accepted = review(result, request(result, metadata={"speaker_member_id": speaker,
        "recorded_at": time(2026), "event_occurred_at": time(2009)}), 0)
    meta = accepted["m4"]["trusted_metadata"]
    assert meta["speaker_member_id"] == speaker
    assert meta["recorded_at"]["value"] == "2026"
    assert meta["event_occurred_at"]["value"] == "2009"


def test_audio_correction_keeps_prediction_and_citation_times():
    from app.ai.grounded_answering import _public_locator
    from app.retrieval.chunking import ChunkSource, chunk_processing_result
    result = extract(wav(), "audio/wav", speech=SyntheticSpeechProvider())
    corrected = dict(result["m4"]["predicted"])
    corrected["transcript"] = "Reviewed transcript"
    corrected["segments"] = [dict(corrected["segments"][0], text="Reviewed transcript")]
    approved = review(result, request(result, "correct", approved=corrected), 0)
    assert approved["m4"]["predicted"]["transcript"] == "Synthetic speech"
    chunks = chunk_processing_result(ChunkSource("room", "record", "file", "job", 0, approved))
    locator = _public_locator(chunks[0].source_locator_json)
    assert locator["anchors"][0]["start_ms"] == 0
    assert locator["anchors"][0]["end_ms"] == 500
    assert locator["review_version"] == 1


def test_no_provider_is_partial_not_fake_success():
    result = extract(png(), "image/png")
    assert result["m4"]["coverage"] == "PARTIAL"
    assert result["m4"]["stages"] == {"ocr": "OCR_ERROR", "visual": "NOT_CONFIGURED"}


def test_video_preserves_failure_in_earlier_frame():
    from app.processing.contracts import ProviderError
    class Decoder:
        extractor_id = "synthetic-video"
        def decode(self, content, mime):
            return DecodedMedia(1000, None, ((0, png()), (500, png())))
    class OCR:
        extractor_id = "synthetic-failure"
        calls = 0
        def extract(self, content, mime):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("OCR_ERROR", "Processing failed.")
            return OCRResult("", 0, "ko")
    result = extract(b"video", "video/mp4", decoder=Decoder(), ocr=OCR(), visual=SyntheticVisualProvider())
    assert result["m4"]["coverage"] == "PARTIAL"
    assert result["m4"]["stages"]["frame_0_ocr"] == "OCR_ERROR"
    assert result["m4"]["stages"]["frame_1_ocr"] == "VALID_MEDIA_NO_OCR_TEXT"


def test_opt_in_empty_tesseract_output(tmp_path):
    from app.processing.local_ocr import _parse
    tsv, txt = tmp_path / "empty.tsv", tmp_path / "empty.txt"
    tsv.write_text("level\ttext\tconf\n1\t\t-1\n")
    txt.write_text("")
    assert _parse(tsv, txt, "kor+eng", allow_empty=True).text == ""
    with pytest.raises(ValueError):
        _parse(tsv, txt, "kor+eng")
    txt.write_text("inconsistent")
    with pytest.raises(ValueError):
        _parse(tsv, txt, "kor+eng", allow_empty=True)


def test_legacy_stt_also_requires_human_review():
    result = {"text": "Unreviewed speech", "extractor": "AUTO_STT", "extractor_id": "synthetic-stt"}
    job = SimpleNamespace(status="SUCCEEDED", processor="stt", attempt=0,
        result_json=canonical_json(result), review_json=None)
    assert not processing_result_is_evidence(job)
    job.review_json = review_digest(result, 0, "approve")
    assert processing_result_is_evidence(job)
    job.review_json = review_digest(result, 0, "reject")
    assert not processing_result_is_evidence(job)


def test_dataset_v1_media_fixtures_are_drafts_and_prediction_does_not_mutate_gold():
    import copy
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((Path(__file__).parents[1] / "app/contracts/dataset_v1.schema.json").read_text())
    for filename in ("images.jsonl", "audio.jsonl", "video.jsonl"):
        entry = json.loads((Path(__file__).parent / "fixtures/m4" / filename).read_text())
        shape = schema["$defs"]["files"]["properties"][filename]
        Draft202012Validator({"$defs": schema["$defs"], **shape}, format_checker=FormatChecker()).validate(entry)
        assert entry["gold"]["annotation_status"] == "DRAFT" and entry["predicted"] == []
        before = copy.deepcopy(entry)
        extract(png(), "image/png", ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider())
        assert entry == before


def test_event_redaction_does_not_mutate_stored_result(monkeypatch):
    from app.services import family_context
    from app.services.multimodal_jobs import visible_result
    result = extract(png(), "image/png", ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider())
    eid = str(uuid4())
    result["m4"]["trusted_metadata"] = {"event_id": eid}
    monkeypatch.setattr(family_context, "accessible_event_ids", lambda *args: [])
    visible = visible_result(result, uid="viewer", room_id="room", db=object())
    assert visible["m4"]["trusted_metadata"]["event_id"] is None
    assert eid not in canonical_json(visible)
    assert result["m4"]["trusted_metadata"]["event_id"] == eid
    assert visible["m4"]["review_source_sha256"] == sha256_text(canonical_json(result))


def test_local_video_command_contract_with_mocked_codec(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from app.processing import multimodal_local
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    calls = []
    def codec(binary, args, **kwargs):
        calls.append((binary, args))
        assert "-protocol_whitelist" in args
        if binary.endswith("ffprobe"):
            return json.dumps({"format": {"duration": "1"},
                "streams": [{"codec_type": "audio"}, {"codec_type": "video"}]}).encode()
        output = Path(args[-1])
        output.write_bytes(wav() if output.suffix == ".wav" else png())
        return b""
    monkeypatch.setattr(multimodal_local, "command", codec)
    decoded = multimodal_local.LocalMediaDecoder(root).decode(b"mocked-codec", "video/mp4")
    assert decoded.duration_ms == 1000 and decoded.audio == wav()
    assert [t for t, _ in decoded.frames] == [0, 500, 900]
    assert len(calls) == 5 and not list(root.iterdir())


def test_partial_approval_requires_explicit_exclusions():
    result = extract(png(), "image/png", ocr=SyntheticImageOCR())
    with pytest.raises(ValueError, match="excluded stages"):
        review(result, request(result), 0)
    approved = review(result, request(result, excluded_stages=["visual"]), 0)
    assert approved["m4"]["approved_excluded_stages"] == ["visual"]
    assert approved["text"] == ""


@pytest.mark.parametrize("mime,extra", [
    ("audio/wav", {"ocr_text": "invented image text"}),
    ("audio/wav", {"visual": {"visual_description": "invented image"}}),
    ("image/png", {"transcript": "invented speech"}),
])
def test_correction_cannot_fabricate_cross_modality_anchors(mime, extra):
    result = extract(wav() if mime == "audio/wav" else png(), mime,
        ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider(), speech=SyntheticSpeechProvider())
    correction = dict(result["m4"]["predicted"], **extra)
    with pytest.raises(ValueError, match="source media type"):
        review(result, request(result, "correct", approved=correction), 0)


def test_legacy_replay_projection_redacts_inaccessible_event(monkeypatch):
    from app.services import family_context, processing_jobs
    from app.services.multimodal_jobs import legacy_visible_job
    result = extract(png(), "image/png", ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider())
    eid = str(uuid4())
    result["m4"]["trusted_metadata"] = {"event_id": eid}
    monkeypatch.setattr(processing_jobs, "serialize_processing_job", lambda job: {"result": result})
    monkeypatch.setattr(family_context, "accessible_event_ids", lambda *args: [])
    visible = legacy_visible_job(object(), "viewer", SimpleNamespace(room_id="room"))
    assert eid not in canonical_json(visible)
    assert result["m4"]["trusted_metadata"]["event_id"] == eid
