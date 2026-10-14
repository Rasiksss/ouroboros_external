"""Строка календаря называет событие, а не поставщика (17.08.2026).

ЗАЧЕМ. Владелец, глядя на боевой календарь, спросил про строку
«Отчётность (Q2 2026) — Finnhub»: что это за отчётность и чьей компании.
Компания стояла в своей колонке, а место осмысленной подписи занимало имя
поставщика — при том что поставщик и так едет в `sources`, а рядом есть
колонка «Источник» со ссылкой.

Своего заголовка Finnhub не отдаёт: в бесплатном тарифе это тикер, дата и
квартал, поэтому подпись собирается кодом — и обязана быть читаемой.

🔴 РЕВИЗИЯ 21.08: ЧТО ИМЕННО ЧИТАЕТСЯ. Прежняя редакция брала
`inspect.getsource(_source_finnhub)`, выдёргивала регуляркой строки
`excerpt = …` и искала в НИХ подстроки. Это охраняло НАПИСАНИЕ выражения, а не
подпись: переименуй `excerpt` в `label` — регулярка вернёт пусто, и тест
покраснеет на здоровом коде; собери подпись двумя присваиваниями или через
хелпер — и «Finnhub» вернётся в неё незамеченным. Теперь ответ Finnhub
подсовывается заглушкой httpx, а проверяется САМО СОБЫТИЕ на выходе.
"""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import date

import pytest

from finengine.skills import ir_calendar
from finengine.skills.ir_calendar import CalendarCompany, CalendarEvent

_COMPANY = CalendarCompany(
    region="US", company="Nvidia", sub_group="Технологии",
    ir_url="https://investor.nvidia.com/",
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Заглушка `httpx.AsyncClient`: отдаёт один ответ календаря Finnhub."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __call__(self, *_args, **_kw) -> "_FakeClient":
        return self

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, _url: str, **_kw) -> _FakeResponse:
        return _FakeResponse(self._payload)


@pytest.fixture()
def finnhub_event(monkeypatch):
    """Одно событие Finnhub, полученное боевым `_source_finnhub`."""
    today = date(2026, 8, 18)
    row = {"date": "2026-08-20", "quarter": 2, "year": 2026, "symbol": "NVDA"}
    monkeypatch.setattr(ir_calendar.settings, "finnhub_api_key", "test-key")
    monkeypatch.setattr(
        ir_calendar.httpx, "AsyncClient",
        _FakeClient({"earningsCalendar": [row]}),
    )
    events = asyncio.run(ir_calendar._source_finnhub([_COMPANY], today))
    assert len(events) == 1, f"событие не собралось: {events}"
    return events[0]


def test_provider_name_is_not_in_the_event_label(finnhub_event: CalendarEvent):
    """Отрицательный контроль: вернул «— Finnhub» в подпись — краснеет."""
    assert "Finnhub" not in finnhub_event.raw_excerpt, (
        "имя поставщика вернулось в подпись события; его место — в `sources` "
        f"и в колонке «Источник»: {finnhub_event.raw_excerpt!r}")
    assert finnhub_event.sources == ["finnhub"], (
        "поставщик пропал из `sources` — колонке «Источник» нечего показать")


def test_label_still_names_the_reporting_period(finnhub_event: CalendarEvent):
    """Квартал и год — единственное содержательное, что даёт Finnhub;
    потерять их значило бы обменять одну беду на другую."""
    assert finnhub_event.raw_excerpt == "Отчётность за Q2 2026 г.", (
        f"подпись события изменилась: {finnhub_event.raw_excerpt!r}")


def test_company_travels_in_its_own_field(finnhub_event: CalendarEvent):
    """Компания не в подписи, а отдельным полем — иначе колонка «Компания»
    осталась бы пустой, а это и был исходный вопрос владельца."""
    assert finnhub_event.company == "Nvidia"
    assert "Nvidia" not in finnhub_event.raw_excerpt, (
        "компания вернулась в подпись — она уже едет своим полем")
    fields = {f.name for f in dataclasses.fields(CalendarEvent)}
    assert {"company", "raw_excerpt", "sources"} <= fields
