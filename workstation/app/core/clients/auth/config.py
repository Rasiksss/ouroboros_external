"""Settings for the external auth validator.

Own `Settings` class so the auth client owns its knobs. Add auth-only env
variables here, not in `app/core/config.py`.
"""

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

    AUTH_ENABLED: bool = False
    AUTH_HOST: str = ""
    AUTH_URI: str = ""
    AUTH_TIMEOUT: float = 10.0


settings = Settings(
    _env_file=[path for path in [environ.get("CONFIG"), ".env"] if path],
)
