"""Чат и кнопка собирают ОДИН И ТОТ ЖЕ выпуск (22.08.2026).

ВОПРОС ВЛАДЕЛЬЦА: «почему на проме не сработал прогон дайджеста у админа?»
Прогон не падал — он отработал успешно, но собрал НЕ ТО.

Живой прогон админа 22.08 14:08→14:15: собрано 3911 новостей при обычных ~900.
В сценарии у него стоит окно 8 часов, а собралось за 48. Причина: кнопка
«Запустить» идёт через раннер сценария и читает его `params`, а инструмент
`daily_news_digest` о сценарии не знал вовсе — у него были свои умолчания:

    since_hours = int(args.get("since_hours", 48))
    top_n       = int(args.get("top_n", 60))

То есть один и тот же дайджест собирался по-разному в зависимости от того,
откуда его позвали, и настройки страницы автоматизаций на чат не действовали.

🔴 И МОЙ СОБСТВЕННЫЙ ПРОМАХ ТОГО ЖЕ ДНЯ. 21.08 я поднял потолок кандидатов до
100 настройкой `news_top_candidates` — и он не доехал: тул и ручка API
передавали `top_n=60` ЯВНО, а явное значение сильнее настройки (так я сам и
написал). В журнале это видно прямо: прогон 21.08 — `cand=100`, прогон 22.08 —
`cand=60`. Ровно та болезнь «умеет, но не доехало», про которую я всю неделю
пишу в комментариях.

Порядок теперь один на оба входа: что попросили в вызове → что сохранил
владелец → умолчание.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.llm.tools import REGISTRY, ToolContext
from finengine.skills.news import pipeline
from finengine.skills.scenarios import store

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



@pytest.fixture()
def owner(tmp_path, monkeypatch):
    """Владелец с сохранёнными настройками дайджеста."""
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "scenarios.db")
    store.init_schema()
    scn = store.create(user_id=7, name="IR Daily", kind="ir_daily")
    store.update(scn.id, params={
        "since_hours": 8,
        "source_groups": {"official": True, "media": False,
                          "telegram": False, "aggregator": False},
        "macro_mode": "week_only",
    })
    return 7


@pytest.fixture()
def seen(monkeypatch):
    """Аргументы, с которыми конвейер РЕАЛЬНО позвали."""
    got: dict = {}

    async def _fake(**kw):
        got.update(kw)
        return {"run_id": "r", "markdown": "#", "items_raw": 0, "items_kept": 0,
                "cost_usd": 0.0, "error": None}

    monkeypatch.setattr(pipeline, "run_daily_news_pipeline", _fake)
    monkeypatch.setattr(pipeline, "get_cached_digest", lambda **kw: None)
    return got


def test_the_chat_tool_uses_the_owners_window(owner, seen):
    """Отрицательный контроль: вернул `args.get("since_hours", 48)` — краснеет.

    Именно так админ получил 48-часовой сбор вместо своих восьми.
    """
    asyncio.run(REGISTRY["daily_news_digest"].handler(
        {"fresh": True}, ToolContext(user_id=owner)))

    assert seen["since_hours"] == 8, (
        f"чат собрал за {seen['since_hours']}ч вместо сохранённых 8 — кнопка и "
        "чат снова дают разный выпуск")


def test_the_chat_tool_uses_the_owners_source_groups_and_macro(owner, seen):
    """Окно — не единственная настройка: группы источников и блок экономики
    тоже сохраняются на странице и тоже игнорировались чатом."""
    asyncio.run(REGISTRY["daily_news_digest"].handler(
        {"fresh": True}, ToolContext(user_id=owner)))

    assert seen["source_groups"] == {"official": True, "media": False,
                                     "telegram": False, "aggregator": False}
    assert seen["macro_mode"] == "week_only"


def test_the_cap_setting_is_not_overridden_by_a_hardcoded_default(owner, seen):
    """🔴 МОЙ ПРОМАХ 21.08: настройка потолка не доезжала, потому что тул слал
    60 явно. Ничего не прося, тул обязан оставить выбор конвейеру."""
    asyncio.run(REGISTRY["daily_news_digest"].handler(
        {"fresh": True}, ToolContext(user_id=owner)))

    assert seen["top_n_candidates"] is None, (
        f"тул снова навязывает потолок {seen['top_n_candidates']} — настройка "
        "news_top_candidates перестала действовать")


def test_an_explicit_argument_still_wins(owner, seen):
    """Настройки владельца — умолчание, а не запрет: если в вызове окно назвали
    явно, побеждает оно. Иначе нельзя будет попросить «за сутки»."""
    asyncio.run(REGISTRY["daily_news_digest"].handler(
        {"fresh": True, "since_hours": 24, "top_n": 40},
        ToolContext(user_id=owner)))

    assert seen["since_hours"] == 24 and seen["top_n_candidates"] == 40


def test_a_user_without_a_scenario_falls_back_to_defaults(seen, tmp_path, monkeypatch):
    """У владельца может не быть сценария дайджеста вовсе — тогда прежние
    умолчания, а не отказ."""
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "empty.db")
    store.init_schema()

    asyncio.run(REGISTRY["daily_news_digest"].handler(
        {"fresh": True}, ToolContext(user_id=999)))

    assert seen["since_hours"] == 48
    assert seen["top_n_candidates"] is None


def test_the_helper_reads_only_the_digest_scenario(owner, tmp_path):
    """У владельца есть и календарь — его параметры (back_days/fwd_days) в
    дайджест попасть не должны."""
    scn = store.create(user_id=owner, name="IR Календарь", kind="ir_calendar")
    store.update(scn.id, params={"back_days": 30, "fwd_days": 30})

    got = pipeline.settings_for_user(owner)
    assert got.get("since_hours") == 8
    assert "back_days" not in got, "в дайджест утекли настройки календаря"
