"""Pydantic schemas for auth endpoints."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from finengine.roles.access import EXTRACTION_ROLES, normalize_account_roles, primary_account_role


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
    role: str = "other"
    roles: list[str] = Field(default_factory=list)
    available_roles: list[str] = Field(default_factory=list)
    # Premium model keys this user can switch to in the model picker.
    # The "Базовая" model is implicit — every user always has it.
    allowed_models: list[str] = Field(default_factory=list)


class InviteOut(BaseModel):
    code: str
    created_at: datetime
    expires_at: Optional[datetime]
    used_by: Optional[str]
    role: str = "other"
    roles: list[str] = Field(default_factory=list)
    allowed_models: list[str] = Field(default_factory=list)
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    email: str = ""
    # Set only on the create response: did the code email go out? (None when
    # listing existing invites — we don't persist per-invite delivery state.)
    email_sent: Optional[bool] = None
    email_status: Optional[str] = None


class CreateInviteIn(BaseModel):
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365)
    # Keys (e.g. ["kimi", "glm"]) of premium models to grant the registered user.
    # Admin sets these via checkboxes when creating the invite. Empty list →
    # user can use the base model only.
    allowed_models: list[str] = Field(default_factory=list)
    # Preferred input. `role` is kept for older frontend/API clients.
    roles: list[str] = Field(default_factory=list)
    role: str = Field(default="other")
    # Invitee contact details. Prefilled from an access request, or typed by the
    # admin. Optional — no required-field validation yet (per product decision).
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    email: str = ""

    @field_validator("roles", mode="before")
    @classmethod
    def _coerce_roles(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [part.strip() for part in v.replace(";", ",").replace("|", ",").split(",") if part.strip()]
        return v

    @model_validator(mode="after")
    def _normalize_roles(self):
        roles = self.roles or self.role
        normalized = normalize_account_roles(roles)
        self.roles = [role for role in normalized if role in EXTRACTION_ROLES]
        self.role = primary_account_role(self.roles)
        return self
