from dataclasses import dataclass
from typing import Protocol


@dataclass
class InferenceRequest:
    model_id: str
    user_query: str
    prompt_package: dict


@dataclass
class InferenceResponse:
    provider: str
    model_id: str
    output_text: str
    mode: str
    done_reason: str | None = None   # runtime stop reason; "length" means the output hit num_predict
    prompt_tokens: int | None = None  # runtime prompt_eval_count, when the provider reports it


class InferenceProvider(Protocol):
    provider_name: str

    def generate(self, request: InferenceRequest) -> InferenceResponse:
        ...
