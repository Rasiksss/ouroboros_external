"""Расписание запускает то и только то, что на него поставили (21.08.2026).

ВОПРОС ВЛАДЕЛЬЦА: «доделай планировщик, у нас крон работает вообще?»

РАБОТАЕТ, И ЕГО ДВА — в этом и была беда.

  * `scenarios/scheduler.py` — настоящий: опрос раз в 30 с по МСК, знает
    hourly/daily/weekly, помнит владельца и его параметры, защищён от повтора
    в том же периоде. Ни один живой сценарий на него не подписан: у всех шести
    freq=manual, то есть автозапуска нет ни у кого.
  * `news/scheduler.py` — legacy: ежедневно в 17:30 звал полный конвейер БЕЗ
    АРГУМЕНТОВ. Окно всегда 48 часов, настройки сценариев игнорировались,
    владелец прогона — None.

Пока чтение готового выпуска было общим, работа второго кому-то доставалась.
После привязки кэша к владельцу его выпуск не отдаётся НИКОМУ: все три читателя
передают реального пользователя. То есть ежедневно оплачивалась классификация
(~$0.07) ради markdown, который писался в базу и не показывался.

Полезной в нём осталась одна часть — прогрев `raw_items`: собранное сырьё
ускоряет последующие ручные прогоны. Её оставили, выпуск убрали.

🔴 И ЛОВУШКА, КОТОРУЮ НАШЁЛ ЗАМЕР. `_is_due`, вызванная напрямую, объявляла
manual-сценарий подлежащим запуску: проверки частоты в ней не было, защита
стояла только в SQL (`list_all_active`). У всех шести живых сценариев
at_time=09:00 — то есть путь в обход запроса запустил бы выпуски людям,
автозапуск не включавшим. Теперь проверка есть в обоих местах.
"""
from __future__ import annotations

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from finengine.skills.news import scheduler as news_sched
from finengine.skills.scenarios import scheduler as sch

_MSK = ZoneInfo("Europe/Moscow")


class _Scn:
    """Минимальный сценарий: планировщик читает только эти поля."""

    def __init__(self, freq: str, at_time: str, last_run_at=None, dows: str = ""):
        self.freq, self.at_time, self.last_run_at = freq, at_time, last_run_at
        self.dow_list = [int(d) for d in dows.split(",") if d.strip().isdigit()]


@pytest.fixture()
def now():
    return datetime(2026, 8, 21, 9, 0, tzinfo=_MSK)


@pytest.mark.parametrize("freq", ["manual", "", "once", "hourly_typo"])
def test_a_non_recurring_scenario_never_fires(freq, now):
    """Отрицательный контроль: убрал проверку частоты — краснеет на manual.

    Именно так выглядят все шесть живых сценариев прода: freq=manual,
    at_time=09:00. Функция без этой проверки объявляла их подлежащими запуску.
    """
    assert sch._is_due(_Scn(freq, "09:00"), now) is False, (
        f"сценарий с freq={freq!r} назначен к запуску — владелец автозапуск "
        "не включал")


def test_a_scheduled_scenario_does_fire(now):
    """Опора: если бы не срабатывало ничего, тест выше был бы зелёным ни о чём."""
    assert sch._is_due(_Scn("daily", "09:00"), now) is True


def test_it_does_not_fire_twice_in_one_period(now):
    """Опрос идёт раз в 30 с, минута длится две итерации — без защиты выпуск
    собирался бы дважды и стоил бы вдвое."""
    already = _Scn("daily", "09:00", last_run_at=now.timestamp())
    assert sch._is_due(already, now) is False


def test_weekly_respects_the_day(now):
    friday = now.weekday()
    assert sch._is_due(_Scn("weekly", "09:00", dows=str(friday)), now) is True
    assert sch._is_due(_Scn("weekly", "09:00", dows=str((friday + 1) % 7)), now) is False


def test_the_legacy_scheduler_only_warms_the_cache():
    """Отрицательный контроль: вернул `run_daily_news_pipeline` — краснеет.

    Его выпуск не читает никто (кэш привязан к владельцу, а у него владельца
    нет), но классификация оплачивалась ежедневно.
    """
    # Смотрим на ВЫЗОВЫ через AST, а не на текст модуля: имя снятой функции
    # осталось в объяснении рядом, и текстовая проверка краснела бы на
    # собственном комментарии — охраняя слова вместо поведения.
    import ast

    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(ast.parse(inspect.getsource(news_sched)))
        if isinstance(n, ast.Call)
    }
    assert "run_daily_news_pipeline" not in called, (
        "планировщик снова собирает полный выпуск, которого никто не увидит — "
        "это оплаченная классификация в никуда")
    assert "collect_all" in called, (
        "снят и прогрев кэша тоже — ручные прогоны станут медленнее без причины")


def test_the_reason_names_the_surviving_part():
    """Строка выглядит как недоделанный вызов и просится «дописать обратно».
    Объяснение, ЧТО именно осталось полезным, должно лежать рядом."""
    src = inspect.getsource(news_sched)
    assert "прогрев" in src and "raw_items" in src, (
        "исчезло объяснение, почему здесь сбор без выпуска")
