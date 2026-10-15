"""Живой прогресс на гейтвей-пути: счётчик «40/77» — УСЛОВИЕ ПРИЁМКИ ВЛАДЕЛЬЦА.

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. `_ProgressBridge` (api/internal_tools.py) — то
единственное звено, через которое прогресс станции доезжает до экрана, когда
ход ведёт петля гейтвея, а не наша. Написан он был 26 июля и на 5 августа
НЕ ИСПОЛНЯЛСЯ НИ РАЗУ: за 48 часов логов dev-бэкенда ноль обращений к
`/internal/tools/call`, а последние сессии гейтвея (25-26.07) не содержат ни
одного тул-события. То есть код выглядел корректным и был непроверяемым.

Владелец поставил условием приёмки перехода: «живой прогресс из станции
продолжает доезжать до экрана». Здесь это проверяется машиной, а не глазами, и
проверяется тем же путём, каким идёт пользователь:

    ToolCallBody → tools_call() → _execute_tool() → _Ctx.progress
      → _ProgressBridge.put_nowait → _progress_events() → emit_to_run()
      → журнал прогона → SSE-подписчик

Подменяется РОВНО ОДНО: тело хендлера тула — вместо многоминутного прогона
движка оно эмитит те же элементы прогресса, что эмитит движок. Всё остальное
звено за звеном настоящее, включая гейт owner-скоупа и белый список.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: занулить `run_id` в `register_session` — событий в
журнале не станет, тесты покраснеют.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.api import internal_tools as it
from finengine.skills import run_orchestrator as ro


@pytest.fixture
async def live_run():
    """Настоящий chat-ран, который живёт, пока идёт вызов тула.

    Фикстура АСИНХРОННАЯ намеренно: `start_run` планирует задачу через
    `asyncio.create_task`, и из синхронной фикстуры это падает «no running
    event loop». Заодно это ровно тот порядок, что на боевом пути — ран
    создаётся внутри обработчика запроса, а не до него.
    """
    stop = asyncio.Event()

    def _runner():
        async def gen():
            yield {"type": "run_started"}
            await stop.wait()
        return gen()

    run_id = ro.start_run(_runner, kind="chat", meta={"user_id": 1, "project_id": "p"})
    yield run_id
    stop.set()


def _stub_tool(name: str, items: list):
    """ToolDef, чей хендлер шлёт в ctx.progress ровно `items`."""
    from finengine.llm.tools import ToolDef

    async def handler(args: dict, ctx) -> str:  # noqa: ANN001, ARG001
        for item in items:
            ctx.progress.put_nowait(item)
        return '{"ok": true}'

    return ToolDef(name=name, description="stub", parameters={"type": "object"},
                   handler=handler)


ENGINE_PROGRESS = [
    {"plan": ["Чтение отчёта", "Извлечение", "Расчёты"]},
    {"step": 0, "status": "done"},
    {"extract_start": {"total": 77}},
    {"metric_row": {"enum": "SALES_REVENUE", "value": 1000}},
    {"metric_row": {"enum": "NET_INCOME", "value": 200}},
    {"extract_done": {"metrics_found": 2, "total": 77}},
]


@pytest.mark.asyncio
async def test_progress_reaches_the_run_journal(live_run, monkeypatch):
    """Главный тест: события движка доезжают до журнала прогона."""
    from finengine.llm import tools as _tools

    await asyncio.sleep(0)  # дать раннеру стартовать
    gw = "agent:aifa-station:openresponses:fe-test"
    it.register_session(gw, user_id=1, project_id="p", run_id=live_run, session_id="")

    monkeypatch.setitem(_tools.REGISTRY, "fe_extract_metrics",
                        _stub_tool("fe_extract_metrics", ENGINE_PROGRESS))
    monkeypatch.setattr(_tools, "_WHITELIST", {"fe_extract_metrics"}, raising=False)

    res = await it.tools_call(
        it.ToolCallBody(tool="fe_extract_metrics", args={}, gateway_session=gw),
        authorization="",
    )
    assert res["ok"] is True

    events = ro.get_run(live_run).events
    kinds = [e.get("type") for e in events]

    # Дробь на экране: extract_start даёт знаменатель, metric_row — числитель.
    assert "extract_start" in kinds, f"нет extract_start — счётчика не будет: {kinds}"
    assert kinds.count("metric_row") == 2, f"строки не доехали: {kinds}"
    assert "extract_done" in kinds, f"нет extract_done — счётчик не закроется: {kinds}"

    start = next(e for e in events if e.get("type") == "extract_start")
    assert start["total"] == 77, "знаменатель дроби потерян"
    done = next(e for e in events if e.get("type") == "extract_done")
    assert done["metrics_found"] == 2

    # Остров «Прогресс»: план и смена статуса шага.
    assert "plan" in kinds, f"остров «Прогресс» пуст: {kinds}"
    plan = next(e for e in events if e.get("type") == "plan")
    assert plan["steps"][0] == "Чтение отчёта"
    assert "plan_step" in kinds, f"шаги не меняют статус: {kinds}"

    # Имя тула обязано ехать с событием — иначе фронт не свяжет его с чипом.
    assert all(e.get("name") == "fe_extract_metrics"
               for e in events if e.get("type") in
               ("extract_start", "metric_row", "extract_done", "plan", "plan_step"))


@pytest.mark.asyncio
async def test_empty_run_id_kills_progress_silently(live_run, monkeypatch):
    """ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: пустой run_id гасит прогресс молча (:81-82).

    Это ровно тот отказ, который на экране выглядит зависшей системой. Тест
    фиксирует поведение как ИЗВЕСТНОЕ: если однажды прогресс пропадёт, первым
    делом смотреть сюда — зарегистрирован ли ход с непустым run_id.
    """
    from finengine.llm import tools as _tools

    await asyncio.sleep(0)
    gw = "agent:aifa-station:openresponses:fe-norun"
    it.register_session(gw, user_id=1, project_id="p", run_id="", session_id="")

    monkeypatch.setitem(_tools.REGISTRY, "fe_extract_metrics",
                        _stub_tool("fe_extract_metrics", ENGINE_PROGRESS))
    monkeypatch.setattr(_tools, "_WHITELIST", {"fe_extract_metrics"}, raising=False)

    await it.tools_call(
        it.ToolCallBody(tool="fe_extract_metrics", args={}, gateway_session=gw),
        authorization="",
    )
    assert not any(e.get("type") == "metric_row" for e in ro.get_run(live_run).events)


@pytest.mark.asyncio
async def test_unregistered_session_is_refused(monkeypatch):
    """Owner-скоуп: чужой/незарегистрированный ключ — 403, а не тихий проход."""
    from fastapi import HTTPException

    from finengine.llm import tools as _tools
    monkeypatch.setitem(_tools.REGISTRY, "fe_extract_metrics",
                        _stub_tool("fe_extract_metrics", []))
    monkeypatch.setattr(_tools, "_WHITELIST", {"fe_extract_metrics"}, raising=False)

    with pytest.raises(HTTPException) as exc:
        await it.tools_call(
            it.ToolCallBody(tool="fe_extract_metrics", args={},
                            gateway_session="agent:aifa-station:subagent:whoever"),
            authorization="",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_progress_ingest_warns_when_nobody_listens(caplog):
    """Дроп прогресса станции обязан быть слышен: раньше он молчал."""
    import logging

    from finengine.api import station_progress_ingest as spi

    with caplog.at_level(logging.WARNING):
        res = await spi.ingest(
            spi.ProgressBatch(nonce="nobody-home", events=[{"type": "metric_row"}]),
            authorization="",
        )
    assert res == {"accepted": 0, "dropped": 1, "reason": "no_consumer"}
    assert "некуда доставить" in caplog.text, \
        "молчаливый дроп вернулся — прогресс пропадёт без следа в логе"
    assert "nobody-home" in caplog.text, "в логе нет нонса — искать будет нечего"
