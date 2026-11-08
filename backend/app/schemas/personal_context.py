from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator


Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class PersonalAliases(BaseModel):
    target_user_id: UUID
    values: list[Alias] = Field(max_length=20)


class PersonalContextUpdate(BaseModel):
    version: int = Field(ge=0)
    memory_note: str = Field(default="", max_length=2000)
    aliases: list[PersonalAliases] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_targets(self):
        targets = [entry.target_user_id for entry in self.aliases]
        if len(targets) != len(set(targets)):
            raise ValueError("Duplicate alias target")
        return self
