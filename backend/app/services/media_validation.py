"""Shared measured multipart upload and media validation policy."""
import asyncio
import io
import logging
from pathlib import Path

from fastapi import HTTPException
from multipart import MultipartParser
from multipart.multipart import parse_options_header
from PIL import Image

from app.services.media_loader import passive_media_type

MAX_OVERHEAD = 65536
MAX_IMAGE_PIXELS = 20 * 1024 * 1024
EXTENSIONS = {
    "text/plain": (".txt",),
    "image/png": (".png",), "image/jpeg": (".jpg", ".jpeg"),
    "image/gif": (".gif",), "image/webp": (".webp",),
    "video/mp4": (".mp4", ".m4v"), "audio/mpeg": (".mp3",), "audio/wav": (".wav",),
}
MIMES = frozenset(EXTENSIONS)


def validate_media(content, mime, filename):
    """Images must structurally verify and fully decode within a pixel budget.

    Audio/video retain supported passive container sniffing; this is not a
    codec decoder or malware scanner. Client MIME and filename never set type.
    """
    if mime == "text/plain" and Path(filename).suffix.lower() == ".txt":
        from app.processing.contracts import ProviderError
        from app.processing.native_text import decode_native_text
        try:
            decode_native_text(content)
        except ProviderError as error:
            raise HTTPException(413 if error.code == "text_too_large" else 400, error.message) from None
        return
    if (not content or mime not in MIMES or
            Path(filename).suffix.lower() not in EXTENSIONS[mime] or
            passive_media_type(content) != mime):
        raise HTTPException(400, "Invalid media upload")
    if not mime.startswith("image/"):
        return
    try:
        # Pillow tolerates a truncated PNG IEND CRC and a missing GIF trailer.
        if mime == "image/png":
            offset = 8
            while offset + 12 <= len(content):
                length = int.from_bytes(content[offset:offset + 4], "big")
                end = offset + 12 + length
                if end > len(content):
                    raise ValueError("Truncated PNG chunk")
                if content[offset + 4:offset + 8] == b"IEND":
                    if length != 0 or content[offset + 8:end] != bytes.fromhex("ae426082"):
                        raise ValueError("Invalid PNG trailer")
                    break
                offset = end
            else:
                raise ValueError("Missing PNG trailer")
        elif mime == "image/gif" and not content.endswith(b";"):
            raise ValueError("Missing GIF trailer")
        with Image.open(io.BytesIO(content), formats=("PNG", "JPEG", "GIF", "WEBP")) as image:
            if Image.MIME[image.format] != mime or image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image format or dimensions")
            image.verify()
        # verify() alone is insufficient: decode every frame with a total bound.
        with Image.open(io.BytesIO(content), formats=("PNG", "JPEG", "GIF", "WEBP")) as image:
            pixels = frame = 0
            while True:
                pixels += image.width * image.height
                if pixels > MAX_IMAGE_PIXELS:
                    raise ValueError("Image frame budget")
                image.load()
                frame += 1
                try:
                    image.seek(frame)
                except EOFError:
                    break
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise HTTPException(400, "Invalid image upload") from None


async def read_media_upload(request, *, max_bytes):
    """Parse one frontend `file` part before any UploadFile/spool dependency runs."""
    if sum(len(k) + len(v) for k, v in request.scope["headers"]) > MAX_OVERHEAD:
        raise HTTPException(413, "Upload too large")
    kind, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary", b"")
    if kind != b"multipart/form-data" or not 1 <= len(boundary) <= 70:
        raise HTTPException(400, "Invalid multipart upload")
    headers, field, value, content = {}, bytearray(), bytearray(), bytearray()
    total = overhead = parts = 0
    ended = False
    mime = filename = None
    def invalid():
        raise HTTPException(400, "Invalid multipart upload")
    def part_begin():
        nonlocal parts
        parts += 1
        if parts != 1:
            invalid()
    def header_data(destination, data, start, end):
        nonlocal overhead
        overhead += end - start
        if overhead > MAX_OVERHEAD:
            raise HTTPException(413, "Upload too large")
        destination.extend(data[start:end])
    def header_end():
        name = bytes(field).lower()
        if name in headers or name not in (b"content-disposition", b"content-type"):
            invalid()
        headers[name] = bytes(value)
        field.clear(); value.clear()
    def headers_finished():
        nonlocal mime, filename
        disposition, attrs = parse_options_header(headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or attrs.get(b"name") != b"file" or b"filename" not in attrs:
            invalid()
        try:
            mime = headers[b"content-type"].decode("ascii").lower()
            filename = attrs[b"filename"].decode("utf-8")
        except (KeyError, UnicodeError):
            invalid()
        if mime not in MIMES:
            invalid()
    def data_received(data, start, end):
        if len(content) + end - start > max_bytes:
            raise HTTPException(413, "Upload too large")
        content.extend(data[start:end])
    def finish():
        nonlocal ended
        ended = True
    parser = MultipartParser(boundary, {
        "on_part_begin": part_begin, "on_header_field": lambda d, s, e: header_data(field, d, s, e),
        "on_header_value": lambda d, s, e: header_data(value, d, s, e), "on_header_end": header_end,
        "on_headers_finished": headers_finished, "on_part_data": data_received, "on_end": finish})
    # The dependency's malformed-header warnings include raw input byte values.
    parser.logger = logging.Logger("secure.multipart", level=logging.CRITICAL + 1)
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                total += len(chunk)
                if total > max_bytes + MAX_OVERHEAD:
                    raise HTTPException(413, "Upload too large")
                # Bound the callback input even when a transport provides one huge chunk.
                for offset in range(0, len(chunk), 65536):
                    parser.write(chunk[offset:offset + 65536])
            parser.finalize()
        claimed = request.headers.get("content-length")
        if claimed is not None and (not claimed.isascii() or not claimed.isdecimal() or int(claimed) != total):
            invalid()
        if not ended or parts != 1 or not content or total - len(content) > MAX_OVERHEAD:
            invalid()
        validate_media(bytes(content), mime, filename)
        return bytes(content), mime
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid multipart upload") from None
    finally:
        content.clear()
