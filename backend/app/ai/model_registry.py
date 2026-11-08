from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ModelProfile:
    id: str
    display_name: str
    provider: str
    placement: str
    supports_image: bool
    supports_audio: bool
    max_context_tokens: int
    notes: str


MODEL_REGISTRY: List[ModelProfile] = [
    ModelProfile(
        id="gemma-4-31b-dgx",
        display_name="Gemma 4 31B DGX",
        provider="ollama",
        placement="dgx_spark",
        supports_image=True,
        supports_audio=False,
        max_context_tokens=8192,
        notes="DGX local Gemma 4 31B; audio records use their transcripts.",
    ),
    ModelProfile(
        id="gemma-4-e2b-device",
        display_name="Gemma 4 E2B",
        provider="gemma",
        placement="device",
        supports_image=True,
        supports_audio=True,
        max_context_tokens=32000,
        notes="Recommended default on-device model for Android-class hardware.",
    ),
    ModelProfile(
        id="gemma-4-e4b-device",
        display_name="Gemma 4 E4B",
        provider="gemma",
        placement="device",
        supports_image=True,
        supports_audio=True,
        max_context_tokens=32000,
        notes="Higher quality on-device option for high-memory devices.",
    ),
    ModelProfile(
        id="exaone-family-vault",
        display_name="EXAONE Family Vault",
        provider="exaone",
        placement="family_vault",
        supports_image=False,
        supports_audio=False,
        max_context_tokens=32768,
        notes="Authoritative family-vault model for canonical archive answers.",
    ),
    ModelProfile(
        id="gemma-3-27b-dgx",
        display_name="Gemma 3 27B DGX",
        provider="ollama",
        placement="dgx_spark",
        supports_image=True,
        supports_audio=False,
        max_context_tokens=128000,
        notes="DGX Spark hosted Gemma 3 27B candidate for higher-quality family archive answers.",
    ),
    ModelProfile(
        id="exaone-35-32b-dgx",
        display_name="EXAONE 3.5 32B DGX",
        provider="ollama",
        placement="dgx_spark",
        supports_image=False,
        supports_audio=False,
        max_context_tokens=32000,
        notes="DGX Spark hosted EXAONE 3.5 32B Korean/English family-vault candidate.",
    ),
    ModelProfile(
        id="exaone-deep-32b-dgx",
        display_name="EXAONE Deep 32B DGX",
        provider="ollama",
        placement="dgx_spark",
        supports_image=False,
        supports_audio=False,
        max_context_tokens=32000,
        notes="DGX Spark hosted EXAONE Deep 32B reasoning-focused candidate.",
    ),
    ModelProfile(
        id="exaone-40-32b-dgx",
        display_name="EXAONE 4.0 32B DGX",
        provider="ollama",
        placement="dgx_spark",
        supports_image=False,
        supports_audio=False,
        max_context_tokens=131000,
        notes="DGX Spark hosted EXAONE 4.0 32B Q4_K_M candidate.",
    ),
    ModelProfile(
        id="exaone-45-33b-dgx",
        display_name="EXAONE 4.5 33B DGX",
        provider="ollama",
        placement="dgx_spark",
        supports_image=False,
        supports_audio=False,
        max_context_tokens=262000,
        notes="DGX Spark hosted EXAONE 4.5 33B Q4_K_M text-route candidate.",
    ),
    ModelProfile(
        id="qwen-25-32b-tuned-dgx",
        display_name="Ambient Qwen 2.5 32B",
        provider="ollama",
        placement="dgx_spark",
        supports_image=False,
        supports_audio=False,
        max_context_tokens=8192,
        notes="Ambient Legacy guardrail-tuned Qwen 2.5 32B Q4_K_M deployment model.",
    ),
    ModelProfile(
        id="qwen-25-72b-dgx",
        display_name="Qwen 2.5 72B DGX",
        provider="ollama",
        placement="dgx_spark",
        supports_image=False,
        supports_audio=False,
        max_context_tokens=32000,
        notes="DGX Spark hosted Qwen 2.5 72B large multilingual baseline.",
    ),
]


def get_model_profile(model_id: str) -> ModelProfile:
    for profile in MODEL_REGISTRY:
        if profile.id == model_id:
            return profile
    raise KeyError(f"Unknown model id: {model_id}")
