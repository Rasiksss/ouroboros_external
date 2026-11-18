"""Settings for the Redis client."""

from __future__ import annotations

from os import environ
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secrets import secman_secrets


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    def __init__(self, **overrides: Any) -> None:
        # secman secrets stay in RAM, never in os.environ — see app/core/secrets.py.
        super().__init__(**{**secman_secrets(), **overrides})

    REDIS_ENABLED: bool = False
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_DEFAULT_TTL: int = 300


settings = Settings(
    _env_file=[path for path in [environ.get("CONFIG"), ".env"] if path],
)
