from pathlib import Path
from typing import Iterable


DEFAULT_PERSONA_FILES = (
    "identity.md",
    "voice.md",
    "timeline.md",
    "memory_policy.md",
)


def load_persona_pack(persona_root: Path, files: Iterable[str] = DEFAULT_PERSONA_FILES) -> str:
    sections = []
    for file_name in files:
        path = persona_root / file_name
        if path.exists():
            sections.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(section for section in sections if section)

