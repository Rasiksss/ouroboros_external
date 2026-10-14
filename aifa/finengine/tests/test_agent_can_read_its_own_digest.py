"""Агент может прочитать СВОЙ ЖЕ собранный выпуск (17.08.2026).

ЗАЧЕМ. Живой ход владельца на проде: он спросил про новость из только что
собранного дайджеста («зачем Claude приобрёл акции Uber»), агент ответил «не
нашёл в дайджесте» и девять раз позвал веб-поиск, пока ход не оборвал
ограничитель на 7 ходов инструментов. Пользователь не получил ничего.

Разбор показал: данные БЫЛИ в базе — заголовок и изложение в `raw_items`, плюс
`one_sentence` и `why_important`, которые написала сама модель при
классификации. Прочитать их было НЕЧЕМ: `grep digest_items|raw_items` по
`llm/tools.py` не находил ничего. Агент сделал единственное, что мог.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finengine.llm.tools import REGISTRY

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



def _call(args: dict) -> dict:
    tool = REGISTRY["fe_news_lookup"]
    return json.loads(asyncio.run(tool.handler(args, None)))


def test_tool_exists_and_is_offered_to_the_agent():
    """Отрицательный контроль: убрал из реестра — краснеет."""
    assert "fe_news_lookup" in REGISTRY
    tool = REGISTRY["fe_news_lookup"]
    assert "query" in tool.parameters["properties"]
    # Описание обязано ВЕЛЕТЬ идти сюда раньше интернета: иначе модель снова
    # выберет привычный веб-поиск, и инструмент останется мёртвым.
    d = tool.description.lower()
    assert "первым" in d and "tavily" in d


def test_it_is_in_the_whitelist():
    """Реестр — ещё не доступность: наружу инструменты пускает белый список.
    Ровно этот разрыв «объявлено, но не доехало» стоил нам #252 и #253."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    wl = yaml.safe_load((root / "deploy" / "tools_whitelist.yaml").read_text(encoding="utf-8"))
    assert "fe_news_lookup" in (wl.get("enabled") or [])


def test_empty_query_is_refused_clearly():
    out = _call({"query": "   "})
    assert "error" in out


def test_nothing_found_says_so_and_forbids_retrying():
    """ГЛАВНОЕ СВОЙСТВО. Модель повторила отказавший поиск девять раз именно
    потому, что отказ не был окончательным по формулировке. Пустой результат
    обязан говорить словами: повторять бесполезно."""
    out = _call({"query": "заведомо-несуществующий-запрос-щщ", "days": 1})
    assert out["found"] == 0
    note = out["note"].lower()
    assert "бесполезно" in note or "бессмысленно" in note


@pytest.mark.parametrize(("days", "limit"), [(0, 0), (999, 999), (-5, -1)])
def test_bounds_are_clamped_not_crashed(days, limit):
    """Модель подставит что угодно — падать нельзя."""
    out = _call({"query": "тест", "days": days, "limit": limit})
    assert "error" not in out or out.get("found") == 0
