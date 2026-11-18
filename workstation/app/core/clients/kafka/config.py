"""Settings for the Kafka client (producer + consumer)."""

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

    KAFKA_ENABLED: bool = False
    KAFKA_TOPIC: str = ""
    KAFKA_BROKERS: str = ""
    KAFKA_BOOTSTRAP_SERVERS: str = ""
    KAFKA_GROUP_ID: str = ""
    KAFKA_CLIENT_ID: str = ""
    KAFKA_OFFSET_RESET_STRATEGY: str = "latest"
    KAFKA_CONSUMER_ENABLED: bool = False
    KAFKA_MAX_REQUEST_SIZE: int = 10 * 1024 * 1024


settings = Settings(
    _env_file=[path for path in [environ.get("CONFIG"), ".env"] if path],
)
