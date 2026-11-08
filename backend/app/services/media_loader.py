"""Reads stored media bytes given a storage locator only — no DB session involved.

Reuses the same local/GCS storage backends as `app.api.routes.uploads`, so
processing jobs read media through the identical path uploads/downloads use.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.core.config import settings
from app.processing.contracts import ProviderError


def _untrusted() -> ProviderError:
    return ProviderError("locator_untrusted", "Stored media locator is not server-owned.")


def _historical_storage() -> ProviderError:
    return ProviderError(
        "historical_storage_unsupported",
        "Historical LEGACY remote storage is unsupported.",
    )


def load_gcs_file(*_args, **_kwargs):
    """Compatibility seam: LEGACY remote reads are permanently fail-closed."""
    raise _historical_storage()


def _record_filename(name: str, record_id: str) -> bool:
    if Path(name).name != name or "\\" in name:
        return False
    stem = Path(name).stem
    if stem == record_id:  # Existing immutable locators remain readable.
        return True
    prefix = record_id + "__"
    if not stem.startswith(prefix):
        return False
    attempt = stem[len(prefix):]
    try:
        return str(UUID(attempt)) == attempt
    except ValueError:
        return False


def resolve_trusted_locator(storage_bucket: str, storage_path: str, *, record_id: str) -> Path:
    """Accept only a server-named LEGACY local object."""
    from app.api.routes.uploads import LOCAL_MEDIA_ROOT
    if storage_bucket == "local-media":
        root = LOCAL_MEDIA_ROOT.resolve()
        candidate = (root / storage_path).resolve()
        if not _record_filename(storage_path, record_id) or candidate.parent != root:
            raise _untrusted()
        return candidate

    if storage_bucket == settings.gcs_bucket_name:
        prefix, _, name = storage_path.partition("/")
        if prefix == "uploads" and name and _record_filename(name, record_id):
            raise _historical_storage()

    raise _untrusted()


def load_media_bytes(storage_bucket: str, storage_path: str, *, record_id: str, max_bytes: int) -> bytes:
    local_path = resolve_trusted_locator(storage_bucket, storage_path, record_id=record_id)
    if not local_path.exists():
        raise ProviderError("media_not_found", "Stored media file was not found.")
    # Bound by recorded size before reading, so an oversized object is never
    # pulled into memory.
    if local_path.stat().st_size > max_bytes:
        raise ProviderError("media_too_large", "Stored media exceeds the processing size limit.")
    with local_path.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ProviderError("media_too_large", "Stored media exceeds the processing size limit.")
    return content


_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")
_AUDIO_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"OggS", b"fLaC")


def media_family(content: bytes) -> frozenset[str] | None:
    """Top-level media family by magic number, independent of client-stored MIME.

    Prefix sniffing only: this detects a container/type contradiction, not
    malicious payloads.
    """
    head = content[:12]
    if head.startswith(_IMAGE_MAGIC):
        return frozenset({"image"})
    if head.startswith(b"RIFF"):
        if head[8:12] == b"WEBP":
            return frozenset({"image"})
        if head[8:12] == b"WAVE":
            return frozenset({"audio"})
        return None
    if head.startswith(_AUDIO_MAGIC):
        return frozenset({"audio"})
    if head[4:8] == b"ftyp":
        return frozenset({"audio", "video"})
    return None


def passive_media_type(content: bytes) -> str | None:
    """Only passive browser media types; never trust stored/client MIME or suffix.

    This is a response classification, not a full media decoder or malware scan.
    Unknown containers and active document formats are always attachments.
    """
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF"):
        if content[8:12] == b"WEBP":
            return "image/webp"
        if content[8:12] == b"WAVE":
            return "audio/wav"
    if content.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return "audio/mpeg"
    if content[4:8] == b"ftyp" and content[8:12] in (
        b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"MSNV",
    ):
        return "video/mp4"
    return None
