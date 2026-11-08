import json
import socket
from urllib import error, request

from app.ai.providers.base import InferenceRequest, InferenceResponse


class OllamaProvider:
    provider_name = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout_seconds: int = 60, model_aliases: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.model_aliases = model_aliases or {}

    def build_payload(self, inference_request: InferenceRequest) -> dict:
        prompt_package = inference_request.prompt_package
        runtime_model_name = self.model_aliases.get(inference_request.model_id, inference_request.model_id)
        payload = {
            "model": runtime_model_name,
            "stream": False,
            "keep_alive": "30m",
            "messages": [
                {
                    "role": "system",
                    "content": prompt_package["instructions"],
                },
                {
                    "role": "user",
                    "content": (
                        f"Persona Markdown:\n{prompt_package['persona_markdown']}\n\n"
                        f"Retrieved Evidence:\n" + "\n".join(prompt_package["retrieved_evidence"]) + "\n\n"
                        f"User Query:\n{inference_request.user_query}"
                    ),
                },
            ],
        }
        if inference_request.model_id == "exaone-45-33b-dgx":
            payload["think"] = False
            payload["options"] = {"num_predict": 512}
        return payload

    def generate(self, inference_request: InferenceRequest) -> InferenceResponse:
        payload = json.dumps(self.build_payload(inference_request)).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, socket.timeout) as exc:
            return InferenceResponse(
                provider=self.provider_name,
                model_id=inference_request.model_id,
                output_text=f"Ollama call failed: {exc}",
                mode="error",
            )

        message = body.get("message", {}) if isinstance(body, dict) else {}
        text = message.get("content") or body.get("response") or body.get("output_text") or ""
        return InferenceResponse(
            provider=self.provider_name,
            model_id=inference_request.model_id,
            output_text=text or "Ollama returned no text.",
            mode="remote",
        )
