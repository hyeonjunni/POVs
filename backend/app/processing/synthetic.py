"""Explicit, inert B synthetic providers. Never selected by production startup."""
from app.processing.contracts import OCRResult, STTResult


class SyntheticOCRProvider:
    extractor_id = "synthetic-ocr-v1"
    language = "ko"

    async def extract(self, content: bytes, mime_type: str):
        return OCRResult("Synthetic OCR", 1.0, "ko")


class SyntheticSTTProvider:
    extractor_id = "synthetic-stt-v1"

    async def transcribe(self, content: bytes, mime_type: str, language="ko"):
        return STTResult("Synthetic STT", language)
