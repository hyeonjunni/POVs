from pydantic import BaseModel, Field


def normalize_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = str(tag or "").strip()
        if not cleaned:
            continue
        dedupe_key = cleaned.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(cleaned)
    return normalized


class UploadCreateRequest(BaseModel):
    room_id: str
    type: str
    title: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class UploadResponse(BaseModel):
    upload_id: str
    room_id: str
    uploader_user_id: str
    type: str
    title: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: str
    created_at: str | None = None
    has_file: bool = False
    file_url: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    file_id: str | None = None
