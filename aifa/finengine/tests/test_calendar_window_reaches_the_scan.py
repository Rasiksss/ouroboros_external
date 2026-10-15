"""«Собери календарь за 1 день» действительно сужает окно (25.08.2026).

ЖАЛОБА ВЛАДЕЛЬЦА: попросил в чате «собери календарь за 1 день» — не сработало.

Не сработать оно и не могло: у инструмента `ir_calendar_run` полей окна НЕ БЫЛО
ВОВСЕ. Модель видела только `companies` и `limit`, выразить период ей было
нечем, а в описании окно стояло константой «±14 дней». Обработчик звал
`build_calendar_stream` без окна, и скан всегда шёл ±14 — в логе прода это
видно по запросам к Finnhub: `from=2026-08-11&to=2026-09-08` при просьбе про
ОДИН день.

Возможность при этом была: движок окно принимает, и в сценарии ручка работает —
замер 21.08 на живом проде дал ±1 → 2 события (разброс −1…0 дн.), ±120 → 8
событий (разброс −113…+90). Не доезжало ровно до чата.

Это та же семья, что съела дайджест дважды на неделе: сначала обработчик не
знал про сценарий, потом умолчание в схеме перебивало настройки. Здесь третий
вид — поля нет вообще. Поэтому сторож проверяет ЦЕПОЧКУ ЦЕЛИКОМ: что схема
поле объявляет, что оно без умолчания, и что значение ДОЕЗЖАЕТ до скана.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.llm import tools as T
from finengine.llm.tools import REGISTRY, ToolContext

_TOOL = "ir_calendar_run"


def _props() -> dict:
    return REGISTRY[_TOOL].parameters["properties"]


@pytest.fixture()
def scan_args(monkeypatch):
    """Аргументы, с которыми РЕАЛЬНО позвали скан календаря."""
    seen: dict = {}

    async def _fake_stream(**kw):
        seen.update(kw)
        yield {"type": "done", "total_events": 0, "total_companies": 0,
               "by_source": {}, "multi_source": 0, "fetch_failed": 0,
               "failed_companies": [], "usage": {}}

    from finengine.skills import ir_calendar
    monkeypatch.setattr(ir_calendar, "build_calendar_stream", _fake_stream)
    monkeypatch.setattr(T, "_calendar_settings_for_user", lambda uid: {})
    return seen


def _run(args: dict, ctx: ToolContext | None = None):
    return asyncio.run(REGISTRY[_TOOL].handler(
        args, ctx or ToolContext(user_id=None, project_id=None)))


def test_the_model_can_express_a_window_at_all():
    """Отрицательный контроль: убрал поля из схемы — краснеет.

    Именно их отсутствие делало просьбу «за 1 день» невыполнимой.
    """
    for key in ("back_days", "fwd_days"):
        assert key in _props(), (
            f"поля «{key}» нет в схеме — модель не сможет сузить окно, "
            "сколько бы её ни просили")


def test_the_schema_advertises_no_default():
    """Урок дайджеста от 24.08: умолчание в схеме модель читает как «передавай
    это значение», и оно перебивает сохранённые настройки владельца."""
    for key in ("back_days", "fwd_days"):
        assert "default" not in _props()[key], (
            f"«{key}» снова объявляет умолчание — оно затрёт настройки владельца")


def test_a_one_day_request_reaches_the_scan(scan_args):
    """Отрицательный контроль: вернул вызов `build_calendar_stream(limit=…,
    names=…)` без окна — краснеет.

    Проверяется ДОШЕДШЕЕ значение, а не наличие поля в схеме: объявить параметр
    и не передать его дальше — ровно тот разрыв, на котором мы горим.
    """
    _run({"back_days": 1, "fwd_days": 1})

    assert scan_args.get("back_days") == 1 and scan_args.get("fwd_days") == 1, (
        f"окно не доехало до скана: {scan_args}")


def test_without_a_window_the_owners_setting_is_used(monkeypatch):
    """Молчание модели — не «14», а «взять сохранённое владельцем»."""
    seen: dict = {}

    async def _fake_stream(**kw):
        seen.update(kw)
        yield {"type": "done", "total_events": 0, "total_companies": 0,
               "by_source": {}, "multi_source": 0, "fetch_failed": 0,
               "failed_companies": [], "usage": {}}

    from finengine.skills import ir_calendar
    monkeypatch.setattr(ir_calendar, "build_calendar_stream", _fake_stream)
    monkeypatch.setattr(T, "_calendar_settings_for_user",
                        lambda uid: {"back_days": 3, "fwd_days": 5})

    _run({}, ToolContext(user_id=7, project_id=None))
    assert seen.get("back_days") == 3 and seen.get("fwd_days") == 5, (
        f"настройки владельца не доехали: {seen}")


def test_an_explicit_window_beats_the_saved_one(monkeypatch):
    """Названный словами период сильнее сохранённого: «за 1 день» должно
    сузить окно даже если в сценарии стоит месяц."""
    seen: dict = {}

    async def _fake_stream(**kw):
        seen.update(kw)
        yield {"type": "done", "total_events": 0, "total_companies": 0,
               "by_source": {}, "multi_source": 0, "fetch_failed": 0,
               "failed_companies": [], "usage": {}}

    from finengine.skills import ir_calendar
    monkeypatch.setattr(ir_calendar, "build_calendar_stream", _fake_stream)
    monkeypatch.setattr(T, "_calendar_settings_for_user",
                        lambda uid: {"back_days": 30, "fwd_days": 30})

    _run({"back_days": 1, "fwd_days": 1}, ToolContext(user_id=7, project_id=None))
    assert seen.get("back_days") == 1 and seen.get("fwd_days") == 1


def test_a_silly_window_is_clamped_not_obeyed(scan_args):
    """Схема ограничивает 1..120, но модель может прислать что угодно —
    обработчик обязан зажимать сам, как это делает страница автоматизаций."""
    _run({"back_days": 9999, "fwd_days": 0})

    assert scan_args.get("back_days") == 120, (
        f"окно вне схемы не зажато: {scan_args}")
    assert scan_args.get("fwd_days") is None, (
        "ноль должен читаться как «не задано», а не как окно в ноль дней")


def test_the_description_does_not_hardcode_the_window():
    """В описании окно стояло константой «±14 дней» — модель и не знала, что
    его можно менять."""
    d = REGISTRY[_TOOL].description
    assert "back_days/fwd_days" in d
    assert "±14 дней" not in d
