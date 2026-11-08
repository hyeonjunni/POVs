from datetime import date
from typing import Literal

from pydantic import BaseModel


class FamilyCreateRequest(BaseModel):
    name: str


class FamilyJoinRequest(BaseModel):
    invite_code: str
    related_to_user_id: str | None = None
    relation_to_related_user: str | None = None


class FamilyResponse(BaseModel):
    room_id: str
    name: str
    invite_code: str
    owner_user_id: str


class FamilyMemberResponse(BaseModel):
    room_id: str
    user_id: str
    role: str
    name: str
    email: str
    profile_image: str | None = None
    age: int | None = None
    gender: str | None = None
    phone: str | None = None
    profile_chunk: str | None = None
    related_to_user_id: str | None = None
    related_to_user_name: str | None = None
    relation_to_related_user: str | None = None


class FamilyDetailResponse(FamilyResponse):
    member_count: int


class FamilyJoinPreviewResponse(FamilyResponse):
    members: list[FamilyMemberResponse]


class FamilyPersonCreateRequest(BaseModel):
    display_name: str
    user_id: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    death_date: date | None = None


class FamilyPersonUpdateRequest(BaseModel):
    display_name: str | None = None
    user_id: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    death_date: date | None = None


class FamilyPersonResponse(BaseModel):
    id: str
    room_id: str
    user_id: str | None
    display_name: str
    gender: str | None
    birth_date: date | None
    death_date: date | None


class FamilyRelationshipCreateRequest(BaseModel):
    from_person_id: str
    to_person_id: str
    relation_type: Literal["PARENT_OF", "SPOUSE_OF"]


class FamilyRelationshipResponse(BaseModel):
    id: str
    room_id: str
    from_person_id: str
    to_person_id: str
    relation_type: str


class KinshipResponse(BaseModel):
    kind: str
    lineage: str
