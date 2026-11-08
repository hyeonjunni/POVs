"""Private OCR working copies; never used for stored/uploaded media.

Regions use EXIF-normalized, unscaled image pixels (top-left origin), not
encoded sensor orientation. Only integer 2x scaling is used, avoiding aspect
ratio rounding. Callers own the private temporary directory and its cleanup.
"""
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from app.core.config import settings
from app.processing.contracts import OCRResult, ProviderError
from app.services.media_validation import MAX_IMAGE_PIXELS


@dataclass(frozen=True)
class OCRWorkingImage:
    width: int
    height: int
    scale: int

    def normalized_regions(self, result: OCRResult) -> OCRResult:
        if self.scale == 1:
            return result
        # Floor the near edge, ceil the far edge to enclose the detected box.
        s = self.scale
        return replace(result, regions=tuple(replace(
            r, left=r.left // s, top=r.top // s,
            width=(r.left + r.width + s - 1) // s - r.left // s,
            height=(r.top + r.height + s - 1) // s - r.top // s,
        ) for r in result.regions))


def prepare_ocr_image(content: bytes, destination: Path) -> OCRWorkingImage:
    """Decode first frame, orient, composite white, grayscale, autocontrast.

    Upscale only plausible small text crops: both sides >=32, longest <=1000.
    Large phone images and tiny icons are not enlarged. Input and output share
    the existing media pixel budget; compressed input retains its byte limit.
    Metadata is discarded so Tesseract cannot apply EXIF orientation twice.
    """
    try:
        if len(content) > settings.ocr_max_input_bytes:
            raise ValueError()
        with Image.open(BytesIO(content)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError()
            source.load()
            oriented = ImageOps.exif_transpose(source)
            rgba = oriented.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            gray = ImageOps.autocontrast(white.convert("L"), cutoff=0)
            width, height = gray.size
            scale = 2 if (min(width, height) >= 32 and max(width, height) <= 1000
                          and width * height * 4 <= MAX_IMAGE_PIXELS) else 1
            if scale == 2:
                gray = gray.resize((width * scale, height * scale), Image.Resampling.LANCZOS)
            gray.info.clear()
            gray.save(destination, format="PNG")
            return OCRWorkingImage(width, height, scale)
    except Exception:
        raise ProviderError("ocr_invalid_image", "OCR image could not be prepared.") from None
