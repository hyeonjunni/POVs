import json
from urllib import error, request

from app.ai.providers.base import InferenceRequest, InferenceResponse


class GemmaProvider:
    provider_name = "gemma"

    def __init__(self, endpoint_url: str | None = None, timeout_seconds: int = 30):
        self.endpoint_url = endpoint_url
        self.timeout_seconds = timeout_seconds

    def build_payload(self, inference_request: InferenceRequest) -> dict:
        prompt_package = inference_request.prompt_package
        prompt_text = (
            f"{prompt_package['instructions']}\n\n"
            f"## Persona Markdown\n{prompt_package['persona_markdown']}\n\n"
            f"## Retrieved Evidence\n" + "\n".join(prompt_package["retrieved_evidence"]) + "\n\n"
            f"## User Query\n{inference_request.user_query}"
        )
        return {
            "model_id": inference_request.model_id,
            "input": prompt_text,
        }

    def generate(self, inference_request: InferenceRequest) -> InferenceResponse:
        if not self.endpoint_url:
            return InferenceResponse(
                provider=self.provider_name,
                model_id=inference_request.model_id,
                output_text="Gemma endpoint is not configured. Returning provider wiring preview only.",
                mode="unconfigured",
            )

        payload = json.dumps(self.build_payload(inference_request)).encode("utf-8")
        req = request.Request(
            self.endpoint_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            return InferenceResponse(
                provider=self.provider_name,
                model_id=inference_request.model_id,
                output_text=f"Gemma endpoint call failed: {exc}",
                mode="error",
            )

        text = body.get("output_text") or body.get("text") or body.get("response") or ""
        return InferenceResponse(
            provider=self.provider_name,
            model_id=inference_request.model_id,
            output_text=text or "Gemma endpoint returned no text.",
            mode="remote",
        )
