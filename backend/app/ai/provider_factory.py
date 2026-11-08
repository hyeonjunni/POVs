from app.ai.model_registry import get_model_profile
from app.ai.providers.base import InferenceProvider
from app.ai.providers.exaone_provider import ExaoneProvider
from app.ai.providers.gemma_provider import GemmaProvider
from app.ai.providers.mock_provider import MockProvider
from app.ai.providers.ollama_provider import OllamaProvider
from app.core.config import settings


def _ollama_model_aliases() -> dict[str, str]:
    return {
        "gemma-4-e2b-device": settings.ollama_gemma_e2b_model_name,
        "gemma-4-e4b-device": settings.ollama_gemma_e4b_model_name,
        "gemma-3-27b-dgx": settings.ollama_gemma_27b_model_name,
        "exaone-35-32b-dgx": settings.ollama_exaone_model_name,
        "exaone-deep-32b-dgx": settings.ollama_exaone_deep_model_name,
        "exaone-40-32b-dgx": settings.ollama_exaone_40_model_name,
        "exaone-45-33b-dgx": settings.ollama_exaone_45_model_name,
        "qwen-25-32b-tuned-dgx": settings.ollama_qwen_32b_tuned_model_name,
        "qwen-25-72b-dgx": settings.ollama_qwen_72b_model_name,
        "exaone-family-vault": settings.ollama_exaone_model_name,
    }


def get_provider_for_model(model_id: str) -> InferenceProvider:
    profile = get_model_profile(model_id)

    if profile.provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            timeout_seconds=settings.ai_provider_timeout_seconds,
            model_aliases=_ollama_model_aliases(),
        )

    if profile.provider == "gemma":
        if settings.prefer_ollama_for_gemma:
            return OllamaProvider(
                base_url=settings.ollama_base_url,
                timeout_seconds=settings.ai_provider_timeout_seconds,
                model_aliases=_ollama_model_aliases(),
            )
        return GemmaProvider(endpoint_url=settings.gemma_endpoint_url, timeout_seconds=settings.ai_provider_timeout_seconds)

    if profile.provider == "exaone":
        if settings.prefer_ollama_for_exaone:
            return OllamaProvider(
                base_url=settings.ollama_base_url,
                timeout_seconds=settings.ai_provider_timeout_seconds,
                model_aliases=_ollama_model_aliases(),
            )
        return ExaoneProvider(endpoint_url=settings.exaone_endpoint_url, timeout_seconds=settings.ai_provider_timeout_seconds)

    return MockProvider()
