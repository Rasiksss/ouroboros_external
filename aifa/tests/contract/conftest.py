"""Контрактные тесты идут на живой ouroboros профиля `contract` и без адреса пропускаются.

Адреса задаёт окружение (PRINCIPLES.md §Б.5): `AIFA_CONTRACT_OURO_URL` — WS ouroboros,
`AIFA_CONTRACT_STUB_URL` — подставная модель. Как поднять профиль — `README.md` рядом.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from stand import OURO_URL_ENV, STUB_URL_ENV, ContractStand
from stub_model.client import StubModelAdmin


@pytest.fixture(scope="session")
def stand() -> ContractStand:
    ouro, stub = os.environ.get(OURO_URL_ENV, ""), os.environ.get(STUB_URL_ENV, "")
    if not (ouro and stub):
        pytest.skip(f"профиль contract не поднят: задайте {OURO_URL_ENV} и {STUB_URL_ENV}")
    return ContractStand(ouro_ws_url=ouro, stub=StubModelAdmin(stub))


@pytest.fixture
def stub(stand: ContractStand) -> Iterator[StubModelAdmin]:
    """Подставная модель с чистым журналом; сценарий загружает сам тест."""
    stand.stub.clear()
    yield stand.stub
