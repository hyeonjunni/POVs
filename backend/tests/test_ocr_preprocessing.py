"""Deterministic working-copy tests; no host OCR accuracy dependency."""
import asyncio
from io import BytesIO
from pathlib import Path
import subprocess
from types import SimpleNamespace

from PIL import Image, ImageOps
import pytest

from app.processing import ocr_preprocessing as pre
from app.processing.contracts import ProviderError
from app.processing.local_ocr import LocalTesseractOCRProvider
from app.services.secure_local_providers import LocalOCR
from test_processing_providers import _write_ocr_success


def encoded(image, orientation=None):
    out = BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(out, format="PNG", exif=exif)
    return out.getvalue()


@pytest.mark.parametrize("orientation", [None, 1, 3, 6, 8])
def test_orientation_and_no_double_rotation(tmp_path, orientation):
    source = Image.new("L", (12, 8), 100)
    source.putpixel((1, 2), 0)
    source.putpixel((10, 6), 255)
    content = encoded(source, orientation)
    destination = tmp_path / "input.png"
    pre.prepare_ocr_image(content, destination)
    with Image.open(BytesIO(content)) as original:
        expected = ImageOps.exif_transpose(original)
        with Image.open(destination) as actual:
            assert actual.size == expected.size
            assert actual.tobytes() == expected.tobytes()
            assert not actual.getexif().get(274)
    first = destination.read_bytes()
    pre.prepare_ocr_image(first, destination)
    assert destination.read_bytes() == first


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_transparency_is_white_before_grayscale(tmp_path, mode):
    im = Image.new("RGBA", (3, 1), (0, 0, 0, 0))
    im.putpixel((1, 0), (0, 0, 0, 255))
    im.putpixel((2, 0), (128, 128, 128, 255))
    im = im.convert(mode)
    path = tmp_path / "input.png"
    pre.prepare_ocr_image(encoded(im), path)
    with Image.open(path) as actual:
        assert actual.mode == "L"
        assert list(actual.tobytes()) == [255, 0, 128]


def test_grayscale_autocontrast(tmp_path):
    im = Image.new("RGB", (2, 1), (100, 100, 100))
    im.putpixel((1, 0), (200, 200, 200))
    path = tmp_path / "input.png"
    pre.prepare_ocr_image(encoded(im), path)
    with Image.open(path) as actual:
        assert actual.mode == "L"
        assert actual.tobytes() == ImageOps.autocontrast(im.convert("L"), cutoff=0).tobytes()


@pytest.mark.parametrize("size,scale", [((99, 53), 2), ((1000, 500), 2),
    ((1001, 500), 1), ((4000, 3000), 1), ((31, 100), 1)])
def test_conditional_scaling_preserves_aspect(tmp_path, size, scale):
    path = tmp_path / "input.png"
    result = pre.prepare_ocr_image(encoded(Image.new("L", size)), path)
    assert result.scale == scale
    with Image.open(path) as actual:
        assert actual.size == (size[0] * scale, size[1] * scale)
        assert actual.width * size[1] == actual.height * size[0]


def test_pixel_and_byte_budgets(tmp_path, monkeypatch):
    content = encoded(Image.new("L", (100, 100)))
    path = tmp_path / "input.png"
    monkeypatch.setattr(pre, "MAX_IMAGE_PIXELS", 10000)
    assert pre.prepare_ocr_image(content, path).scale == 1
    monkeypatch.setattr(pre, "MAX_IMAGE_PIXELS", 9999)
    with pytest.raises(ProviderError):
        pre.prepare_ocr_image(content, path)
    monkeypatch.setattr(pre.settings, "ocr_max_input_bytes", 1)
    with pytest.raises(ProviderError):
        pre.prepare_ocr_image(content, path)


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("outcome", ["ok", "failure", "timeout", "mismatch", "malformed"])
def test_providers_share_preparation_argv_geometry_and_cleanup(tmp_path, monkeypatch, secure, outcome):
    tmp_path.chmod(0o700)
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    original_stat = Path.stat
    def safe_binary(path, *args, **kwargs):
        if str(path) == "/usr/bin/tesseract":
            return SimpleNamespace(st_uid=0, st_mode=0o755)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", safe_binary)
    content = encoded(Image.new("RGBA", (100, 50), (0, 0, 0, 0)), 6)
    expected = tmp_path / "expected.png"
    pre.prepare_ocr_image(content, expected)
    working_bytes = expected.read_bytes()
    expected.unlink()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        source = Path(argv[1])
        assert source.read_bytes() == working_bytes
        assert source.suffix == ".png"
        assert source.parent.stat().st_mode & 0o777 == 0o700
        assert argv[3:] == ["-l", "kor+eng", "--psm", "6", "tsv", "txt"]
        assert kwargs["shell"] is False
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if outcome == "failure":
            return SimpleNamespace(returncode=1)
        result = _write_ocr_success(argv, **kwargs)
        if outcome == "mismatch":
            Path(argv[2]).with_suffix(".txt").write_text("mismatch")
        return result
    monkeypatch.setattr(subprocess, "run", run)
    provider = LocalOCR(tmp_path) if secure else LocalTesseractOCRProvider(executable="tesseract")
    def extract():
        data = b"malformed" if outcome == "malformed" else content
        result = provider.extract(data, "image/png")
        return asyncio.run(result) if secure else result
    if outcome == "ok":
        result = extract()
        assert result.text == "안녕하세요\n"
        assert result.confidence == 0.9175
        assert (result.regions[0].left, result.regions[0].top,
                result.regions[0].width, result.regions[0].height) == (5, 10, 15, 8)
    else:
        with pytest.raises(ProviderError):
            extract()
    assert bool(calls) == (outcome != "malformed")
    assert list(tmp_path.iterdir()) == []


def test_partial_alpha_compositing(tmp_path):
    im = Image.new("RGBA", (3, 1), (0, 0, 0, 128))
    im.putpixel((0, 0), (0, 0, 0, 255))
    im.putpixel((2, 0), (255, 255, 255, 255))
    path = tmp_path / "input.png"
    pre.prepare_ocr_image(encoded(im), path)
    with Image.open(path) as actual:
        assert actual.tobytes() == bytes([0, 127, 255])


def test_region_mapping_encloses_odd_edges():
    from app.processing.contracts import OCRRegion, OCRResult
    original = OCRResult("한글", 0.9, "kor+eng", (OCRRegion("한글", 0.9, 3, 5, 4, 6),))
    result = pre.OCRWorkingImage(100, 50, 2).normalized_regions(original)
    assert result.regions == (OCRRegion("한글", 0.9, 1, 2, 3, 4),)
    assert result.text == original.text and result.confidence == original.confidence


def test_psm_setting_is_bounded_and_configurable():
    from app.core.config import Settings
    from pydantic import ValidationError
    assert Settings(_env_file=None, ocr_psm=3).ocr_psm == 3
    for value in (-1, 14):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, ocr_psm=value)
