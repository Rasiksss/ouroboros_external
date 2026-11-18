"""Shared response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HealthzResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    database: str = "ok"
    redis: str = "ok"
    runtime: str = "ok"


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Стабильный машинно-читаемый код ошибки.")
    message: str = Field(description="Человекочитаемое описание ошибки.")
    details: dict[str, Any] = Field(default_factory=dict, description="Структурированные детали ошибки.")
