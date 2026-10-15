"""Ревью части 14 доводки: остановка пользователем (Р14.3).

Остановка хода закрывает поток событий, но рабочий поток дорабатывает сам (``live_stream``). Если за это
время первая попытка упала, цикл не запускается: он тратил бы модель на ответ, который никто не прочтёт.
"""
# ruff: noqa: F811 — фикстуры подключены импортом, как в соседних тестах
from __future__ import annotations

import asyncio
import threading

from finengine.orchestrator.live_stream import stream_blocking
from finengine.skills.production_runtime import execute_routed_goal

import test_planning_failure_entry as entry
from test_chat_outcome_contract import _arm_dynamic, _give_up
from test_execution_failure_entry import read_value, script, single_metric  # noqa: F401 — фикстуры

spy = entry.spy
budgets = entry.budgets


# ---- остановка пользователем: поток дорабатывает, но цикл не стартует --------------------------------

def test_stream_blocking_tells_the_worker_when_the_consumer_has_left() -> None:
    seen: dict[str, object] = {}
    started = threading.Event()
    release = threading.Event()

    def work(*, event_sink, is_cancelled):
        event_sink("first")
        started.set()
        release.wait(5)
        seen["cancelled"] = is_cancelled()

    async def main() -> None:
        stream = stream_blocking(work, cancel_kw="is_cancelled")
        await stream.__anext__()
        await stream.aclose()  # пользователь нажал «стоп»
        release.set()

    asyncio.run(main())
    for _ in range(100):
        if "cancelled" in seen:
            break
        threading.Event().wait(0.05)

    assert seen == {"cancelled": True}


def test_a_stopped_turn_does_not_start_the_cycle_after_the_first_attempt_fails(
    base, script, spy, budgets,
) -> None:
    script.fail_first = frozenset({"SALES_REVENUE"})
    consumer_left = threading.Event()
    finished = threading.Event()
    stopped_with: list[BaseException] = []

    def route(*, event_sink, **kwargs):
        def wait_for_the_stop(event: dict) -> None:
            event_sink(event)
            consumer_left.wait(10)  # рабочий поток стоит на первом событии, пока потребитель не ушёл

        try:
            execute_routed_goal(event_sink=wait_for_the_stop, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — остановка приходит как BaseException, читать её некому
            stopped_with.append(exc)
        finally:
            finished.set()

    async def main() -> None:
        stream = stream_blocking(
            route, cancel_kw="is_cancelled", owner_id=entry.OWNER, case_id="chat-case",
            document_id="chat-doc", document_text="financial report", goal=single_metric(), store=base.store,
        )
        await stream.__anext__()
        await stream.aclose()  # пользователь нажал «стоп» на первом событии
        consumer_left.set()

    asyncio.run(main())

    assert finished.wait(30)
    assert not spy.calls, "остановленный ход не тратит модель на цикл (Р14.3)"
    assert [type(exc) for exc in stopped_with] == [asyncio.CancelledError], stopped_with


def test_a_stopped_turn_does_not_ask_the_planner_once_the_cycle_has_started(
    base, script, budgets, monkeypatch,
) -> None:
    """Пользователь уходит, когда цикл уже начался (карточка входа): ни один ход модели не запрашивается."""
    script.fail_first = frozenset({"SALES_REVENUE"})
    planner = _arm_dynamic(monkeypatch, [_give_up("не нужно")])
    consumer_left = threading.Event()
    finished = threading.Event()
    stopped_with: list[BaseException] = []

    def route(*, event_sink, **kwargs):
        def wait_for_the_stop(event: dict) -> None:
            event_sink(event)
            if event.get("type") == "dynamic_move":
                consumer_left.wait(10)

        try:
            execute_routed_goal(event_sink=wait_for_the_stop, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — остановка приходит как BaseException
            stopped_with.append(exc)
        finally:
            finished.set()

    async def main() -> None:
        stream = stream_blocking(
            route, cancel_kw="is_cancelled", owner_id=entry.OWNER, case_id="chat-case",
            document_id="chat-doc", document_text="financial report", goal=single_metric(), store=base.store,
        )
        async for kind, payload in stream:
            if kind == "event" and payload.get("type") == "dynamic_move":
                break  # пользователь нажал «стоп» на карточке входа
        await stream.aclose()
        consumer_left.set()

    asyncio.run(main())

    assert finished.wait(30)
    assert planner.prompts == [], "остановленный цикл не тратит модель"
    assert [type(exc) for exc in stopped_with] == [asyncio.CancelledError], stopped_with
