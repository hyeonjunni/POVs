from app.ai.providers.base import InferenceRequest, InferenceResponse
from app.ai.providers.exaone_provider import ExaoneProvider
from app.core.config import settings


class ExaoneClient:
    def __init__(self):
        self.provider = ExaoneProvider(endpoint_url=settings.exaone_endpoint_url)

    def summarize(self, text: str) -> str:
        response = self.provider.generate(
            InferenceRequest(
                model_id="exaone-family-vault",
                user_query=text,
                prompt_package={
                    "instructions": "Summarize the following text.",
                    "persona_markdown": "",
                    "retrieved_evidence": [text],
                },
            )
        )
        return response.output_text
