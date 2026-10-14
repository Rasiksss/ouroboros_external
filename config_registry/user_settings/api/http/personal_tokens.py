from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

PERSONAL_TOKEN_MAX_LENGTH = 8192
PERSONAL_TOKEN_MIN_TTL_SEC = 60
PERSONAL_TOKEN_MAX_TTL_SEC = 7_776_000


class SetPersonalTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: str = Field(repr=False)
    ttl_sec: int = Field(ge=PERSONAL_TOKEN_MIN_TTL_SEC, le=PERSONAL_TOKEN_MAX_TTL_SEC)


__all__ = [
    "PERSONAL_TOKEN_MAX_LENGTH",
    "PERSONAL_TOKEN_MAX_TTL_SEC",
    "PERSONAL_TOKEN_MIN_TTL_SEC",
    "SetPersonalTokenRequest",
]
