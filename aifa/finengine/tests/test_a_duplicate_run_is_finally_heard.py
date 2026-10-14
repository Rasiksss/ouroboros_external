"""Второй прогон по тому же документу перестал уходить в тишину.

ЦЕНА, ИЗМЕРЕННАЯ И ОПЛАЧЕННАЯ (#160). Дубль прогона по Мечелу сжёг **253,5
секунды** — движок посчитал один и тот же отчёт дважды. Сторож дубля завели
18.08… наполовину: `run_started` / `run_finished` писали состояние, а
`run_status` не читал НИКТО. Запись велась в пустоту, и узнать о повторе было
неоткуда — ни пользователю, ни нам.

Это тот же класс, что «правило в одном месте из двух», только злее: здесь
второго места не было вовсе, а первое выглядело работающим.

⚠️ ГОВОРИМ, А НЕ ОТКАЗЫВАЕМ, И ЭТО РЕШЕНИЕ. Отказать вслепую значит однажды
убить ЗАКОННЫЙ повтор — перезапуск после сбоя, второй ход по тому же отчёту с
другим составом показателей. Порога «сколько повторов ненорма» назначать не на
чем, пока их никто не считал. Тот же порядок принят для сорвавшихся батчей:
сначала запись, потом неделя наблюдений, потом правило.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · снять ветку `prev["state"] == "running"` → падает
    `test_the_second_run_on_the_same_document_is_counted`;
  · считать дублем ЛЮБОЙ предыдущий прогон (`if prev is not None`) → падает
    `test_a_run_after_a_finished_one_is_not_a_duplicate`. ⚠️ Первая мутация
    была слабее — снимала только ранний выход для `done`/`failed`, и все шесть
    оставались зелёными: состояние `done` всё равно не проходит проверку
    `== "running"`. Мутация должна ломать ПРЕДМЕТ, а не соседнюю строку;
  · не чистить счётчик в сборке мусора → падает
    `test_the_counter_does_not_outlive_the_run_it_counts`.
"""
from __future__ import annotations

import pytest

from finengine.skills import grounding_bus as bus

_OWNER = 7
_DOC = "doc-мечел"


@pytest.fixture(autouse=True)
def _clean():
    bus._runs.clear()
    bus._dup_runs.clear()
    yield
    bus._runs.clear()
    bus._dup_runs.clear()


def test_a_single_run_is_not_a_duplicate():
    bus.run_started(_OWNER, _DOC, source="worker")
    assert bus.duplicate_runs(_OWNER, _DOC) == 0


def test_the_second_run_on_the_same_document_is_counted(caplog):
    """Главный случай: пока первый считает, пришёл второй."""
    bus.run_started(_OWNER, _DOC, source="worker")
    with caplog.at_level("WARNING"):
        bus.run_started(_OWNER, _DOC, source="station")
    assert bus.duplicate_runs(_OWNER, _DOC) == 1, (
        "повтор по тому же документу не посчитан — запись сторожа снова "
        "ведётся в пустоту, как было с 18.08")
    assert any("ДУБЛЬ ПРОГОНА" in r.message for r in caplog.records), (
        "о дубле не сказано в лог — узнать о нём по-прежнему неоткуда")


def test_a_run_after_a_finished_one_is_not_a_duplicate():
    """Прогон кончился — следующий законен и повтором не считается.

    Иначе счётчик наполнился бы обычной работой: один отчёт обрабатывают
    несколькими ходами подряд (извлекли, посчитали, разобрали).
    """
    bus.run_started(_OWNER, _DOC, source="worker")
    bus.run_finished(_OWNER, _DOC, source="worker", summary={"metrics": 110})
    bus.run_started(_OWNER, _DOC, source="station")
    assert bus.duplicate_runs(_OWNER, _DOC) == 0


def test_another_document_is_counted_separately():
    bus.run_started(_OWNER, _DOC, source="worker")
    bus.run_started(_OWNER, _DOC, source="station")
    bus.run_started(_OWNER, "другой-документ", source="station")
    assert bus.duplicate_runs(_OWNER, _DOC) == 1
    assert bus.duplicate_runs(_OWNER, "другой-документ") == 0


def test_another_owner_is_counted_separately():
    """Чужой прогон по тому же документу — не наш дубль."""
    bus.run_started(_OWNER, _DOC, source="worker")
    bus.run_started(8, _DOC, source="worker")
    assert bus.duplicate_runs(_OWNER, _DOC) == 0
    assert bus.duplicate_runs(8, _DOC) == 0


def test_the_counter_does_not_outlive_the_run_it_counts(monkeypatch):
    """Счётчик чистится вместе с прогоном, а не растёт вечно."""
    bus.run_started(_OWNER, _DOC, source="worker")
    bus.run_started(_OWNER, _DOC, source="station")
    assert bus.duplicate_runs(_OWNER, _DOC) == 1

    monkeypatch.setattr(bus, "_MEMORY_TTL_SEC", -1.0)   # всё протухло
    bus._runs_gc()
    assert bus.duplicate_runs(_OWNER, _DOC) == 0, (
        "счётчик пережил прогон, которого касается, — он будет расти вечно")
