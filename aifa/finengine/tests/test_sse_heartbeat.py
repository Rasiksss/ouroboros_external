"""Кадр-биение в потоке хода чата: длинная ШТАТНАЯ пауза не роняет подписку.

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. 05.08 ход 2ae08a6a сервер довёл до `done` и
сохранил артефакт, а пользователь увидел «Load failed»: поток молчал, потому
что хвост подписки — безусловное `await q.get()`, и 80% времени хода (замер
02.08: сбор показателей 193 с, написание анализа 176 с) в сокет не уходит ни
байта. Молчащее соединение рвут сторож простоя во фронте (5 мин) и
промежуточные NAT/прокси (от 60 с) — раньше, чем ход доходит до ответа.

Проверяется ТЕМ ЖЕ путём, каким идёт пользователь:

    run_orchestrator.start_run(kind="chat")            ← настоящий прогон
      → GET /chat/runs/{id}/stream (chat_run_stream)   ← настоящая ручка
      → run_orchestrator.subscribe(heartbeat_sec=…)    ← настоящий хвост
      → кадры `data: {…}` из StreamingResponse         ← то, что видит браузер

Подменяется РОВНО ОДНО — период биения (боевые 15 с ждать в тесте незачем).
Ни ручка, ни подписка, ни формат кадра не трогаются.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в run_orchestrator.subscribe безусловное
`ev = await q.get()` (или убрать `heartbeat_sec=` из ручки) — оба теста
краснеют по таймауту ожидания кадра.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finengine.auth.db import User
from finengine.skills import run_orchestrator as ro


# Период биения в тесте. Держим сильно меньше боевых 15 с, но заметно больше
# накладных расходов цикла событий, чтобы «пришёл пинг» не путалось с гонкой.
TEST_HEARTBEAT = 0.2


@pytest.fixture
def fast_heartbeat(monkeypatch):
    """Ручка читает период из модуля в момент вызова — подменяем его там же."""
    monkeypatch.setattr(ro, "HEARTBEAT_SEC", TEST_HEARTBEAT)


def _user() -> User:
    """Владелец прогона. Гейт owner-скоупа в ручке настоящий — пользователь
    обязан совпасть с meta.user_id, иначе ручка отдаст 404."""
    return User(id=1, username="test", password_hash="x", is_admin=False)


async def _open_stream(run_id: str):
    """Настоящая ручка подписки. Возвращает итератор кадров SSE."""
    from finengine.api import main as api_main  # noqa: PLC0415

    resp = await api_main.chat_run_stream(run_id, user=_user())
    return resp.body_iterator.__aiter__()


async def _next_event(it, timeout: float) -> dict:
    """Следующий кадр потока, разобранный как событие. Кадр — ровно то, что
    уходит в сокет: `data: {json}\\n\\n`."""
    chunk = await asyncio.wait_for(it.__anext__(), timeout=timeout)
    raw = chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
    assert raw.startswith("data: ") and raw.endswith("\n\n"), raw
    return json.loads(raw[len("data: ") : -2])


def _silent_run(release: asyncio.Event) -> str:
    """Настоящий chat-прогон, который штатно молчит: одно событие, потом
    многоминутная (здесь — управляемая) пауза, потом `done`."""

    def _runner():
        async def gen():
            yield {"type": "text_delta", "text": "начал"}
            await release.wait()
            yield {"type": "done"}

        return gen()

    return ro.start_run(_runner, kind="chat", meta={"user_id": 1, "project_id": "p"})


async def test_ping_arrives_while_runner_is_silent(fast_heartbeat):
    """Раннер молчит — подписчик всё равно получает кадры."""
    release = asyncio.Event()
    run_id = _silent_run(release)
    it = await _open_stream(run_id)
    try:
        seen: list[dict] = []
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            seen.append(await _next_event(it, timeout=2.0))
            if seen[-1].get("type") == "ping":
                break
        assert any(e.get("type") == "ping" for e in seen), (
            f"за 5 с молчания раннера биения не пришло: {seen}"
        )
        # Биение не притворяется работой: настоящие события раннера на месте
        # и ничем не подменены.
        assert {"type": "text_delta", "text": "начал"} in seen

        # Пинг не оседает ни в памяти прогона, ни в журнале на диске —
        # иначе реплей после перезагрузки тащил бы его пользователю, а аудит
        # хода врал бы о содержании.
        state = ro.get_run(run_id)
        assert all(e.get("type") != "ping" for e in state.events)
        path = ro.event_file(run_id, "chat")
        if path.exists():
            assert '"ping"' not in path.read_text(encoding="utf-8")
    finally:
        release.set()
        await it.aclose()


async def test_ping_stops_after_done(fast_heartbeat):
    """Биение не мешает завершению и не течёт после `done`: поток кончается."""
    release = asyncio.Event()
    run_id = _silent_run(release)
    it = await _open_stream(run_id)

    # Дожидаемся хотя бы одного биения — иначе проверка «прекратилось» пуста.
    while True:
        ev = await _next_event(it, timeout=2.0)
        if ev.get("type") == "ping":
            break

    release.set()

    after_done: list[dict] = []
    done_seen = False
    with pytest.raises(StopAsyncIteration):
        while True:
            # Запас времени — много больше периода биения: если пинг течёт
            # после `done`, сюда прилетит он, а не StopAsyncIteration.
            ev = await _next_event(it, timeout=TEST_HEARTBEAT * 10)
            if done_seen:
                after_done.append(ev)
            if ev.get("type") == "done":
                done_seen = True

    assert done_seen, "прогон не дошёл до done"
    assert after_done == [], f"после done в поток утекли кадры: {after_done}"
