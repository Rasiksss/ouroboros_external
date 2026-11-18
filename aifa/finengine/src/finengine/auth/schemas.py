"""Pydantic schemas for auth endpoints."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# Username constraints: 3–32 chars, ascii letters / digits / dot / underscore /
# dash. Rejects spaces, slashes, anything URL-unsafe.
_USERNAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(min_length=8, max_length=32)

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, v: str) -> str:
        v = v.strip().lower()
        if any(c not in _USERNAME_OK for c in v):
            raise ValueError("username may contain only letters, digits, dot, underscore, dash")
        return v


class LoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, v: str) -> str:
        return v.strip().lower()


class UserOut(BaseModel):
    username: str
    is_admin: bool
    # Premium model keys this user can switch to in the model picker.
    # The "Базовая" model is implicit — every user always has it.
    allowed_models: list[str] = Field(default_factory=list)


class InviteOut(BaseModel):
    code: str
    created_at: datetime
    expires_at: Optional[datetime]
    used_by: Optional[str]
    allowed_models: list[str] = Field(default_factory=list)


class CreateInviteIn(BaseModel):
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365)
    # Keys (e.g. ["kimi", "glm"]) of premium models to grant the registered user.
    # Admin sets these via checkboxes when creating the invite. Empty list →
    # user can use the base model only.
    allowed_models: list[str] = Field(default_factory=list)
