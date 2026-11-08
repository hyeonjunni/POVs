"""Token budget for the single record-answer prompt sent to the local Gemma runtime.

Counts come from the runtime model's own tokenizer (app.ai.gemma_tokenizer, read from the
Ollama GGUF; validated equal to Ollama's prompt_eval_count). /api/chat adds a constant
CHAT_OVERHEAD (BOS, turn markers, empty thought block) around the two stripped messages.
If that tokenizer is unavailable, UTF-8 byte length is used instead: every non-special token
covers at least one byte, so the fallback is a strict upper bound, never an under-count.
"""
import json
import os
import time
from dataclasses import dataclass

RUNTIME_NUM_CTX = 8192  # LocalInference options["num_ctx"]; the model itself allows more, the runtime does not.
# LocalInference record_answer options["num_predict"]. It also bounds generation time: measured on the DGX
# gemma4:31b runtime (2026-10-29), prefill runs at ~900 tok/s and decode at ~10.9 tok/s near a full context, so a
# full prompt plus 512 output tokens took ~56 s, past the provider's 55 s deadline, which fail-stops the backend.
# 384 keeps that worst case near 45 s. A cold model load is not covered by this bound.
OUTPUT_RESERVE = 384
PREFILL_TOKENS_PER_S, DECODE_TOKENS_PER_S = 900, 10.9
MAX_CITATIONS = 50      # SecureAskResponse/app contract: an answer carries at most 50 citations


def chat_overhead():
    from app.ai.gemma_tokenizer import CHAT_OVERHEAD
    return CHAT_OVERHEAD


def count_tokens(text):
    """(tokens, exact). exact=False means the byte-length upper bound was used."""
    from app.ai import gemma_tokenizer
    try:
        if gemma_tokenizer.available():
            return gemma_tokenizer.count_tokens(text), True
    except Exception:
        pass
    return len(text.encode("utf-8")), False


@dataclass(frozen=True)
class Sizes:
    fixed: dict
    evidence: tuple[int, ...]
    exact: bool


def measure(*, evidence, **fixed):
    """Count each named prompt component (system, persona, history, question, tool_results, ...)."""
    exact = True
    sizes = {}
    for name, value in fixed.items():
        parts = value if isinstance(value, (list, tuple)) else [value]
        total = 0
        for part in parts:
            n, ok = count_tokens(part)
            total, exact = total + n, exact and ok
        sizes[name] = total
    counted = []
    for item in evidence:
        n, ok = count_tokens(item)
        counted.append(n)
        exact = exact and ok
    return Sizes(sizes, tuple(counted), exact)


def prompt_tokens(system, user):
    """Exact runtime count of the assembled /api/chat prompt (Ollama strips message content)."""
    s, s_ok = count_tokens(system.strip())
    u, u_ok = count_tokens(user.strip())
    return s + u + chat_overhead(), s_ok and u_ok


def log(record):
    """Numbers only (no text, ids or names); enabled by AMBIENT_CONTEXT_BUDGET_LOG on a candidate."""
    path = os.environ.get("AMBIENT_CONTEXT_BUDGET_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": round(time.time(), 3), **record}) + "\n")
    except OSError:
        pass
