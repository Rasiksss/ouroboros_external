"""Неизменяемый выпуск рыночного сценария (ADR-0098)."""
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import NotRequired, TypedDict

from finengine.contracts.base import Contract, NonEmptyStr, new_id


class DigestNews(TypedDict):
    title: NonEmptyStr
    summary: NonEmptyStr
    url: NonEmptyStr
    published_at: NonEmptyStr
    source_id: NonEmptyStr
    category: Literal["macro", "regulator", "corporate", "unknown"]


class DigestMacro(TypedDict):
    id: NonEmptyStr
    ru_name: NonEmptyStr
    region: NonEmptyStr
    unit: NonEmptyStr
    source_url: NonEmptyStr
    latest_date: NonEmptyStr
    latest_value: float
    compares: list[dict[str, str | float]]
    published: str
    fetched_at: float


class DigestSourceStats(TypedDict):
    total: int
    failed: int
    raw: NotRequired[int]
    candidates: NotRequired[int]


class DigestParameters(Contract):
    since_hours: int = Field(default=48, ge=6, le=336, strict=True)


class DigestOutput(Contract):
    schema_version: Literal["1.0"] = "1.0"
    output_type: Literal["DigestOutput"] = "DigestOutput"
    output_id: str = Field(default_factory=new_id)
    run_id: str = Field(default_factory=new_id)
    window_start: datetime
    window_end: datetime
    collected_at: datetime
    markdown: NonEmptyStr
    status: Literal["complete", "partial", "empty"]
    news: list[DigestNews] = Field(default_factory=list)
    macro: dict[str, DigestMacro] = Field(default_factory=dict)
    source_stats: DigestSourceStats = Field(default_factory=lambda: DigestSourceStats(total=0, failed=0))
    warnings: list[str] = Field(default_factory=list)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)

    @property
    def evidence_id(self) -> str:
        return self.output_id

    @model_validator(mode="after")
    def valid_window(self) -> "DigestOutput":
        dates = (self.window_start, self.window_end, self.collected_at)
        if any(value.tzinfo is None for value in dates) or self.window_start > self.window_end:
            raise ValueError("окно дайджеста требует упорядоченные даты с часовым поясом")
        return self
