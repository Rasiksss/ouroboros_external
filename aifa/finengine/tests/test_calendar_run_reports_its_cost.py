"""Календарь записывает, во сколько обошёлся (21.08.2026).

ВОПРОС ВЛАДЕЛЬЦА: «сколько в деньгах мы тратим на один дейли или календарь».
По дайджесту ответ был из журнала — `digest_runs.cost_usd`. По календарю
ответить было нечем: он звал `llm.complete`, которая usage не возвращает, и
расход не записывался НИГДЕ. Пришлось считать по токенам и опубликованному
тарифу — то есть отвечать оценкой там, где должен быть факт.

Разница не косметическая: календарь делает ОТДЕЛЬНЫЙ вызов модели на каждую из
89 компаний (дайджест — один на весь батч), и по расчёту выходит примерно в
восемь раз дороже дайджеста. Величина, которая настолько больше и при этом не
измеряется, рано или поздно удивит.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from finengine.skills import ir_calendar as ic


@pytest.fixture(autouse=True)
def _fresh_counter():
    ic._usage_var.set({"in": 0, "out": 0, "cost_usd": 0.0, "calls": 0})


def _company() -> ic.CalendarCompany:
    return ic.CalendarCompany(region="US", company="Nvidia", sub_group="Tech",
                              ir_url="https://investor.nvidia.com/")


def test_extraction_adds_its_usage_to_the_run(monkeypatch):
    """Отрицательный контроль: вернул `llm.complete` без учёта — счётчик
    остаётся нулевым, и стоимость прогона снова неизвестна."""
    async def _fake(**kw):
        return "[]", {"in": 1200, "out": 300, "cost_usd": 0.0021}

    monkeypatch.setattr(ic.llm, "complete_with_usage", _fake)
    asyncio.run(ic._llm_extract(_company(), "текст страницы", date.today(), "ir_site"))

    u = ic._usage()
    assert u["calls"] == 1, "вызов не посчитан"
    assert u["in"] == 1200 and u["out"] == 300
    assert abs(u["cost_usd"] - 0.0021) < 1e-9


def test_usage_accumulates_across_companies(monkeypatch):
    """Календарь зовёт модель на КАЖДУЮ компанию — счёт должен складываться,
    иначе в журнал попадёт цена одной страницы вместо всего прогона."""
    async def _fake(**kw):
        return "[]", {"in": 1000, "out": 200, "cost_usd": 0.001}

    monkeypatch.setattr(ic.llm, "complete_with_usage", _fake)

    async def _three():
        for _ in range(3):
            await ic._llm_extract(_company(), "текст", date.today(), "ir_site")

    asyncio.run(_three())
    u = ic._usage()
    assert u["calls"] == 3 and u["in"] == 3000
    assert abs(u["cost_usd"] - 0.003) < 1e-9


def test_a_failed_extraction_costs_nothing(monkeypatch):
    """Отказ модели не должен приписывать прогону расход — иначе цена поедет
    вверх на каждом недоступном сайте."""
    async def _boom(**kw):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(ic.llm, "complete_with_usage", _boom)
    asyncio.run(ic._llm_extract(_company(), "текст", date.today(), "ir_site"))

    assert ic._usage()["calls"] == 0


def test_the_run_reports_usage_outward():
    """Счётчик бесполезен, если не доезжает до вызывающего: событие `done`
    обязано нести расход, а инструмент — отдавать его наружу."""
    import inspect

    from finengine.llm import tools

    assert '"usage": _usage()' in inspect.getsource(ic.build_calendar_stream), (
        "событие done не несёт расход прогона")
    assert 'done.get("usage"' in inspect.getsource(tools._h_ir_calendar_run), (
        "инструмент не отдаёт расход наружу — журнал снова пуст")


def test_cost_is_computed_when_the_provider_reports_none(monkeypatch):
    """🔴 ГЛАВНОЕ ПРО ЦЕНУ: прокси по этому пути `cost_usd` НЕ ВОЗВРАЩАЕТ.

    Замер на проде 21.08: 5 вызовов, 12 101 входящий токен, 8 914 исходящих —
    и `cost_usd: 0.0`. Если довериться провайдеру, в журнал попадёт ноль, то
    есть учёт будет выглядеть заведённым, а цифра останется выдумкой. Считаем
    тем же калькулятором, что и дайджест: одна цена на два продукта, иначе их
    не сравнить.

    Отрицательный контроль: убрать расчёт и складывать только `cost_usd`
    провайдера — тест краснеет с нулём.
    """
    async def _no_cost(**kw):
        return "[]", {"in": 12101, "out": 8914, "cost_usd": 0.0,
                      "model": "qwen/qwen3.6-plus"}

    monkeypatch.setattr(ic.llm, "complete_with_usage", _no_cost)
    asyncio.run(ic._llm_extract(_company(), "текст", date.today(), "ir_site"))

    got = ic._usage()["cost_usd"]
    assert got > 0, (
        "цена прогона нулевая при 21 015 токенах — провайдер её не отдаёт, "
        "а мы не посчитали")
    from finengine.llm.pricing import cost_usd
    expected = cost_usd({"model": "qwen/qwen3.6-plus", "in": 12101, "out": 8914})
    assert abs(got - expected) < 1e-9, (
        f"цена {got} не совпала с общим калькулятором {expected} — у календаря "
        "свой способ считать деньги")


def test_provider_cost_wins_when_it_is_reported():
    """Если провайдер цену ВСЁ ЖЕ отдал — берём её, а не свой расчёт: она
    точнее, потому что учитывает скидки и кэш."""
    import asyncio as _a

    async def _with_cost(**kw):
        return "[]", {"in": 100, "out": 50, "cost_usd": 0.0042,
                      "model": "qwen/qwen3.6-plus"}

    ic._usage_var.set({"in": 0, "out": 0, "cost_usd": 0.0, "calls": 0})
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(ic.llm, "complete_with_usage", _with_cost)
    _a.run(ic._llm_extract(_company(), "текст", date.today(), "ir_site"))
    mp.undo()
    assert abs(ic._usage()["cost_usd"] - 0.0042) < 1e-9, (
        "расчёт подменил цену, которую отдал провайдер")
