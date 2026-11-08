"""M4.1 transport, schema and timestamp boundaries; no model dependency."""
import json
import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest
from app.core.config import Settings
from app.processing import multimodal_providers as providers
from app.processing.contracts import ProviderError
from app.services.multimodal_jobs import extract_configured
from test_m4_multimodal import png, wav


@pytest.fixture
def config(tmp_path):
    tmp_path.chmod(0o700)
    return Settings(_env_file=None, media_speech_python=sys.executable,
        media_speech_model_path=str(tmp_path), media_speech_device="cpu")


def response(content):
    return {"model": "gemma3:27b", "done": True, "message": {"content": content}}


def test_visual_structure_optional_fields_and_safe_observation(config, tmp_path, monkeypatch, caplog):
    # Earlier migration tests use logging.fileConfig(disable_existing_loggers=True).
    monkeypatch.setattr(providers.logger, "disabled", False)
    calls = []
    def worker(*args):
        calls.append(args)
        return response(json.dumps({"visual_description": "PRIVATE description", "objects": ["cup"], "people_count": 0}))
    monkeypatch.setattr(providers, "_worker", worker)
    caplog.set_level(logging.INFO, logger=providers.__name__)
    result = providers.OllamaVisualProvider(config, tmp_path, "opaque-attempt:2").analyze(png(), "image/png")
    assert result.objects == ["cup"] and result.people_count == 0
    assert result.location_hint is None and result.generic_people_descriptions == []
    body = calls[0][2]["body"]
    assert body["format"]["additionalProperties"] is False
    assert body["messages"][1]["images"] and body["model"] == config.media_visual_model
    assert "PRIVATE" not in caplog.text and "opaque-attempt:2" in caplog.text
    assert "SUCCEEDED" in caplog.text and "duration_ms" in caplog.text


@pytest.mark.parametrize("raw", ['not JSON', '[]', '{}', '{"visual_description":null}',
    '{"visual_description":"x","people_count":"4"}',
    '{"visual_description":"x","member_id":"person"}',
    '{"visual_description":"x","objects":[42]}'])
def test_bad_visual_output_never_becomes_prediction(config, tmp_path, monkeypatch, raw):
    monkeypatch.setattr(providers, "_worker", lambda *args: response(raw))
    with pytest.raises(ProviderError, match="Invalid structured"):
        providers.OllamaVisualProvider(config, tmp_path).analyze(png(), "image/png")


def test_single_json_fence_compatibility_still_validates(config, tmp_path, monkeypatch):
    raw = '```json\n{"visual_description":"A cup"}\n```'
    monkeypatch.setattr(providers, "_worker", lambda *a: response(raw))
    assert providers.OllamaVisualProvider(config, tmp_path).analyze(png(), "image/png").visual_description == "A cup"
    raw += '\nOther commentary'
    with pytest.raises(ProviderError):
        providers.OllamaVisualProvider(config, tmp_path).analyze(png(), "image/png")


@pytest.mark.parametrize("endpoint", ["https://example.com", "http://localhost:11434", "http://127.0.0.1:11434/x",
    "http://user:secret@127.0.0.1", "http://192.168.1.2", "http://127.0.0.1?key=secret"])
def test_non_loopback_or_credential_endpoint_rejected(endpoint):
    with pytest.raises(ProviderError):
        providers.local_endpoint(endpoint)


@pytest.mark.parametrize("kind", ["visual", "speech"])
@pytest.mark.parametrize("failure", ["timeout", "exit", "json"])
def test_worker_failure_is_sanitized_and_no_retry(tmp_path, monkeypatch, kind, failure):
    tmp_path.chmod(0o700)
    calls = []
    def process(*args, **kwargs):
        calls.append((args, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired("PRIVATE", 1)
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout=b"PRIVATE invalid")
    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(ProviderError) as error:
        providers._worker(sys.executable, kind, {}, tmp_path, 1)
    assert "PRIVATE" not in str(error.value) and len(calls) == 1
    env = calls[0][1]["env"]
    assert env["HF_HUB_OFFLINE"] == "1" and "HTTP_PROXY" not in env


def test_incomplete_visual_response_is_not_admitted(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0,
        stdout=b'{"done":false,"message":{"content":"partial"}}'))
    with pytest.raises(ProviderError):
        providers._worker(sys.executable, "visual", {}, tmp_path, 1)


def test_uncertain_visual_call_poison_secure_guard(tmp_path, monkeypatch):
    from app.services.secure_runtime import RuntimeFailStop, _current_guard
    tmp_path.chmod(0o700)
    stopped = []
    guard = SimpleNamespace(poisoned=False, runtime=SimpleNamespace(local_provider_admission=True,
        fail_stop=lambda: stopped.append(True)))
    token = _current_guard.set(guard)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b""))
    try:
        with pytest.raises(RuntimeFailStop):
            providers._worker(sys.executable, "visual", {}, tmp_path, 1)
        assert guard.poisoned and stopped == [True]
    finally:
        _current_guard.reset(token)


def test_timestamp_mapping_and_language():
    value = {"text": "One Two", "language": "en", "segments": [
        {"text": "One", "start": 0.12, "end": 0.56}, {"text": "Two", "start": 0.56, "end": 0.98}]}
    result = providers.speech_result(value, 1000)
    assert result.language == "en" and result.text == "One Two"
    assert [(round(s.start_seconds * 1000), round(s.end_seconds * 1000)) for s in result.segments] == [(120, 560), (560, 980)]


@pytest.mark.parametrize("segments", [[], [{"text": "x"}],
    [{"text": "x", "start": -0.1, "end": 0.5}],
    [{"text": "x", "start": 0, "end": float("nan")}],
    [{"text": "x", "start": 0, "end": 2}],
    [{"text": "x", "start": 0.2, "end": 0.1}],
    [{"text": "x", "start": 0, "end": 0.5}, {"text": "x", "start": 0.4, "end": 0.8}]])
def test_missing_invalid_or_overlapping_timestamps_fail(segments):
    with pytest.raises(ProviderError):
        providers.speech_result({"text": "x", "language": "en", "segments": segments}, 1000)


def test_pcm_only_scope_and_offline_provider_mapping(config, tmp_path, monkeypatch):
    calls = []
    def worker(*args):
        calls.append(args)
        return {"text": "Speech", "language": "en", "segments": [{"text": "Speech", "start": 0.1, "end": 0.7}]}
    monkeypatch.setattr(providers, "_worker", worker)
    provider = providers.TransformersWhisperProvider(config, tmp_path)
    result = provider.transcribe(wav(), "audio/wav")
    assert result.text == "Speech" and calls[0][2]["model"] == str(tmp_path)
    with pytest.raises(ProviderError) as error:
        provider.transcribe(b"ID3", "audio/mpeg")
    assert error.value.code == "UNSUPPORTED_MEDIA" and len(calls) == 1


def test_admission_precedes_provider_construction(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "secure_provider_profile", "trusted-dgx-local")
    monkeypatch.setattr(providers, "OllamaVisualProvider", lambda *a: pytest.fail("provider constructed"))
    with pytest.raises(ProviderError):
        extract_configured(png(), "image/png", runtime=SimpleNamespace(local_provider_admission=False))


def test_textless_ocr_and_visual_are_separate(tmp_path, config, monkeypatch):
    from app.processing.multimodal import SyntheticImageOCR, extract
    monkeypatch.setattr(providers, "_worker", lambda *a: response('{"visual_description":"A cup","objects":["cup"]}'))
    result = extract(png(), "image/png", ocr=SyntheticImageOCR(), visual=providers.OllamaVisualProvider(config, tmp_path))
    media = result["m4"]
    assert media["media_metadata"]["ocr_outcome"] == "OCR_EMPTY_SUCCESS"
    assert media["predicted"]["visual"]["objects"] == ["cup"]
    assert media["coverage"] == "COMPLETE" and media["approved"] is None and result["text"] == ""
