import pytest

from app.ai.providers.base import InferenceRequest
from app.services import secure_local_providers as local


def test_record_answer_uses_gemma31b(monkeypatch):
    def post(path, payload):
        assert path == "/api/chat"
        assert payload["model"] == "gemma4:31b"
        assert payload["options"]["num_ctx"] == 8192
        assert payload["think"] is False
        assert "format" not in payload
        return {"model": "gemma4:31b", "message": {"content": "기록을 확인했습니다."}}

    monkeypatch.setattr(local, "_post", post)
    request = InferenceRequest("gemma-4-31b-dgx", "무슨 일이 있었어?", {
        "response_mode": "record_answer", "instructions": "한국어로 답하세요.",
        "persona_markdown": "", "retrieved_evidence": ["가족과 여행했다."],
    })
    assert local.LocalInference().generate(request).output_text == "기록을 확인했습니다."
    monkeypatch.setattr(local, "_post", lambda *args: {"model": "gemma4:e2b"})
    with pytest.raises(local.LocalProviderUnavailable):
        local.LocalInference().generate(request)
