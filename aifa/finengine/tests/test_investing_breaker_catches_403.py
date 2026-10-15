"""Предохранитель Investing размыкается на 403, а не только на 429 (17.08.2026).

ЗАЧЕМ. Замер на проде 17.08: 57 отказов Investing за трое суток, и ВСЕ — 403,
ни одного 429. Предохранитель проверял только 429, поэтому не сработал ни разу,
и каждый прогон впустую ходил за всеми показателями этого источника — с паузой
4 с между вызовами и таймаутом 20 с на каждый.

Отсюда же и наблюдение владельца, что блок экономики полупустой: из Investing
берутся 23 показателя из 53 — PMI, ВВП, промпроизводство, безработица, PPI и
розница по четырём регионам.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.skills.news import macro_indicators as mi


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code
        self.text = ""


class _Client:
    """Считает походы наружу — на них и держится смысл предохранителя."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.calls = 0

    async def get(self, *a, **kw):  # noqa: ANN002, ANN003
        self.calls += 1
        return _Resp(self.code)


@pytest.fixture(autouse=True)
def _reset_breaker():
    mi._investing_trip_state.update({"streak": 0, "tripped": False})
    yield
    mi._investing_trip_state.update({"streak": 0, "tripped": False})


def _spec():
    return next(s for s in mi._REGISTRY if s.provider == "investing")


@pytest.mark.parametrize("code", [403, 429])
def test_breaker_trips_on_both_refusal_codes(code):
    """Отрицательный контроль: вернул условие к `== 429` — случай 403 краснеет."""
    client = _Client(code)
    spec = _spec()

    async def _run() -> None:
        for _ in range(mi._INVESTING_TRIP_COUNT + 3):
            await mi._fetch_investing(client, spec)

    asyncio.run(_run())
    assert mi._investing_trip_state["tripped"], f"HTTP {code} не разомкнул цепь"
    assert client.calls == mi._INVESTING_TRIP_COUNT, (
        f"после размыкания ушло {client.calls} запросов вместо "
        f"{mi._INVESTING_TRIP_COUNT} — впустую ходим наружу")


def test_success_resets_the_streak():
    """Разомкнуть цепь имеют право только ОТКАЗЫ ПОДРЯД: один 403 между
    удачными заборами не должен гасить источник на весь цикл."""
    spec = _spec()

    class _Alternating:
        def __init__(self) -> None:
            self.n = 0

        async def get(self, *a, **kw):  # noqa: ANN002, ANN003
            self.n += 1
            return _Resp(403 if self.n % 2 else 200)

    client = _Alternating()

    async def _run() -> None:
        for _ in range(8):
            await mi._fetch_investing(client, spec)

    asyncio.run(_run())
    assert not mi._investing_trip_state["tripped"], "цепь разомкнулась на череде"


def test_other_error_codes_do_not_trip_it():
    """500 — сбой на их стороне, он проходит и лечится сам. Размыкать цепь на
    нём значило бы гасить источник из-за одной плохой минуты."""
    client = _Client(500)
    spec = _spec()

    async def _run() -> None:
        for _ in range(mi._INVESTING_TRIP_COUNT + 2):
            await mi._fetch_investing(client, spec)

    asyncio.run(_run())
    assert not mi._investing_trip_state["tripped"]
