from pydantic import BaseModel, EmailStr, Field
from typing import Literal


class SignUpRequest(BaseModel):
    username: str = Field(min_length=4, max_length=24)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr


class LoginRequest(BaseModel):
    username: str = Field(min_length=4, max_length=24)
    password: str = Field(min_length=8, max_length=128)


class UserProfileUpdateRequest(BaseModel):
    name: str
    age: int
    gender: str
    phone: str
    email: EmailStr


class UserResponse(BaseModel):
    user_id: str
    username: str | None = None
    email: EmailStr
    name: str
    profile_image: str | None = None
    age: int | None = None
    gender: str | None = None
    phone: str | None = None
    profile_chunk: str | None = None


class AuthSessionResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class ActivationStatus(BaseModel):
    operation_id: str
    kind: Literal["SIGNUP", "ROOM_CREATE"]
    state: Literal["WAITING_CREDENTIAL", "READY", "COMMITTED", "EXPIRED", "FAILED"]
    expires_at: int
    retry_after_seconds: Literal[2] | None
