"""SECURE ask must never turn a failed provider into a successful answer."""
import json
import subprocess
from dataclasses import replace

import pytest

from test_secure_catalogue import catalogue as catalogue
from test_secure_identity import identity_client as identity_client
from test_secure_media import media_client as media_fixture
from test_secure_retrieval import processed, index_secure, ask_secure
from app.retrieval.evaluation import SyntheticTokenHashProvider
from app.services import secure_local_providers as local

media_client = media_fixture


@pytest.fixture
def answer_client(media_client, monkeypatch):
    c = media_client
    row, done = processed(c, monkeypatch)
    runtime = c.app.state.secure_runtime
    runtime.embedding_provider = SyntheticTokenHashProvider()
    assert index_secure(c, row, done).status_code == 200
    runtime.inference_provider = local.LocalInference()
    runtime.local_provider_admission = True
    def unexpected_transport(*args, **kwargs):
        pytest.fail("Provider tests must stub the local transport")
    monkeypatch.setattr(local.subprocess, "run", unexpected_transport)
    return c


@pytest.mark.parametrize("content", [
    "not-json PRIVATE", '{}', '{"claim_ids":[]}', '{"claim_ids":["unknown"]}',
    '{"claim_ids":["claim-1"],"answer":"PRIVATE"}',
    '{"claim_ids":["claim-1"],"claim_ids":["claim-1"]}',
])
def test_retained_legacy_selection_rejects_invalid_output(answer_client, monkeypatch, content):
    from app.api.routes import grounded_answering as routes
    original = routes.generate_grounded_answer
    monkeypatch.setattr(routes, "generate_grounded_answer", lambda snapshot, **kw:
                        original(replace(snapshot, record_discovery=False), **kw))
    raw = json.dumps({"model": "gemma4:31b", "message": {"content": content}}).encode()
    monkeypatch.setattr(local.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 0, raw, b""))
    response = ask_secure(answer_client, arm="bm25")
    assert response.status_code == 503, response.text
    assert "PRIVATE" not in response.text and "citations" not in response.text


@pytest.mark.parametrize("envelope", [{}, {"model":"wrong", "message":{"content":"PRIVATE"}},
                                     {"model":"gemma4:31b", "message":{"content":123}}])
def test_invalid_local_envelope_is_explicit_failure(answer_client, monkeypatch, envelope):
    raw = json.dumps(envelope).encode()
    monkeypatch.setattr(local.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 0, raw, b""))
    response = ask_secure(answer_client, arm="bm25")
    assert response.status_code == 503, response.text
    assert "PRIVATE" not in response.text


def test_missing_provider_is_explicit_failure(answer_client):
    del answer_client.app.state.secure_runtime.inference_provider
    response = ask_secure(answer_client, arm="bm25")
    assert response.status_code == 503, response.text


def test_valid_real_provider_prose_is_reported_local(answer_client, monkeypatch):
    calls = []
    def run(cmd, **kw):
        calls.append(json.loads(kw["input"]))
        raw = json.dumps({"model":"gemma4:31b", "message":{"content":"아버지 검진은 오후 2시였습니다."}}).encode()
        return subprocess.CompletedProcess(cmd, 0, raw, b"")
    monkeypatch.setattr(local.subprocess, "run", run)
    response = ask_secure(answer_client, arm="bm25")
    assert response.status_code == 200, response.text
    assert response.json()["provider_mode"] == "local"
    assert response.json()["answer"] == "아버지 검진은 오후 2시였습니다."
    assert len(response.json()["citations"]) == 1
    assert calls[0][0] == "/api/chat" and calls[0][1]["model"] == "gemma4:31b"
    assert calls[0][1]["options"]["num_ctx"] == 8192
    assert "format" not in calls[0][1]
    assert len(calls) == 1


@pytest.mark.parametrize("output, mode", [("", "local"), ("  \n", "local"), ("PRIVATE", "error")])
def test_empty_or_error_answer_is_explicit_failure(answer_client, monkeypatch, output, mode):
    from app.ai.providers.base import InferenceResponse
    monkeypatch.setattr(local.LocalInference, "generate", lambda self, request:
                        InferenceResponse("local", request.model_id, output, mode))
    response = ask_secure(answer_client, arm="bm25")
    assert response.status_code == 503, response.text
    assert "PRIVATE" not in response.text and "citations" not in response.text


@pytest.mark.parametrize("failure", ["timeout", "unavailable", "malformed-json"])
def test_uncertain_transport_preserves_failstop(answer_client, monkeypatch, failure):
    from app.services.secure_runtime import RuntimeFailStop
    runtime = answer_client.app.state.secure_runtime
    stops = []
    runtime.fail_stop = lambda: stops.append("stop")
    def run(cmd, **kw):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"PRIVATE")
        return subprocess.CompletedProcess(cmd, 1 if failure == "unavailable" else 0, b"PRIVATE", b"")
    monkeypatch.setattr(local.subprocess, "run", run)
    with pytest.raises(RuntimeFailStop):
        ask_secure(answer_client, arm="bm25")
    assert stops == ["stop"]


def test_provider_exception_is_explicit_failure(answer_client, monkeypatch):
    def unavailable(self, request):
        raise RuntimeError("PRIVATE provider failure")
    monkeypatch.setattr(local.LocalInference, "generate", unavailable)
    response = ask_secure(answer_client, arm="bm25")
    assert response.status_code == 503 and "PRIVATE" not in response.text


def test_secure_renderer_refuses_fallback_result(answer_client, monkeypatch):
    from app.ai.grounded_answering import GroundedAnswerResult
    from app.api.routes import grounded_answering as routes
    def fallback(snapshot, **kwargs):
        return GroundedAnswerResult(snapshot, tuple(c.claim_id for c in snapshot.plan.claims), "fallback")
    monkeypatch.setattr(routes, "generate_grounded_answer", fallback)
    from app.ai.providers.base import InferenceResponse
    monkeypatch.setattr(local.LocalInference, "generate", lambda self, request:
                        InferenceResponse("local", request.model_id, "PRIVATE", "local"))
    response = ask_secure(answer_client, arm="bm25")
    assert response.status_code == 503, response.text
    assert "PRIVATE" not in response.text
