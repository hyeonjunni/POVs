"""Deterministic UTF-8 evidence extraction; original bytes remain immutable."""
import hashlib
import unicodedata
from typing import Final, Literal, TypedDict

from app.core.config import settings
from app.processing.contracts import ProviderError
from app.retrieval.chunking import normalize_text
from app.services.media_loader import media_family

EXTRACTOR_ID: Final = "native_utf8_nfkc_ws_v1"


class NativeTextResult(TypedDict):
    text: str
    extractor: Literal["NATIVE_TEXT"]
    extractor_id: str
    source_sha256: str
    user_confirmed: bool
    review_status: Literal["unreviewed"]


def decode_native_text(content: bytes) -> str:
    """Reject binary/control input before deterministic Unicode normalization."""
    if len(content) > settings.text_max_input_bytes:
        raise ProviderError("text_too_large", "Text source exceeds the size limit.")
    if media_family(content) is not None:
        raise ProviderError("invalid_text", "Text source must be UTF-8 plain text.")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProviderError("invalid_text", "Text source must be UTF-8 plain text.") from None
    if any(unicodedata.category(char) == "Cc" and char not in "\t\r\n" for char in decoded):
        raise ProviderError("invalid_text", "Text source contains unsupported control characters.")
    normalized = normalize_text(decoded)
    if not normalized:
        raise ProviderError("invalid_text", "Text source must not be empty.")
    return normalized


def extract_native_text(content: bytes, mime_type: str) -> NativeTextResult:
    """Produce unreviewed common-pipeline evidence without any model provider."""
    if mime_type != "text/plain":
        raise ProviderError("media_content_mismatch", "Stored text has an invalid media type.")
    normalized = decode_native_text(content)
    if len(normalized.encode("utf-8")) > settings.processing_max_output_bytes:
        raise ProviderError("text_output_too_large", "Processed text exceeds the output limit.")
    return NativeTextResult(text=normalized, extractor="NATIVE_TEXT", extractor_id=EXTRACTOR_ID,
                            source_sha256=hashlib.sha256(content).hexdigest(),
                            user_confirmed=False, review_status="unreviewed")
