"""Адреса профиля `contract`, каталог сценариев и то, что тесты получают от фикстуры."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stub_model.client import StubModelAdmin

OURO_URL_ENV = "AIFA_CONTRACT_OURO_URL"
STUB_URL_ENV = "AIFA_CONTRACT_STUB_URL"
SCENARIOS = Path(__file__).parent / "scenarios"


@dataclass(frozen=True)
class ContractStand:
    ouro_ws_url: str
    stub: StubModelAdmin
