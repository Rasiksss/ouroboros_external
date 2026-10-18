"""Корень хода из вызова инструмента доходит до `turn_gateway` (шаг 08 → 09, P52).

Настоящие `TurnGateway` и `tool_api` на подставном ouroboros. Без названного корня шлюз принимает
за финал первый же непрогрессный кадр своего хода — в том числе итог подзадачи. Поэтому корень
считается названным, когда кадр подзадачи, пришедший раньше финала, не кончает ход.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fake_ouroboros import FakeOuroboros, assistant, echo
from tool_harness import (  # noqa: F401 — `coordination`: фикстура окружения
    ROOT,
    Plugin,
    coordination,
    ctx_of,
    first_line,
)
from turn_harness import TurnRunner, gateway_of
from websockets.asyncio.server import ServerConnection

from finengine.assistant.goal_result import GoalResult
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_record import TurnRecord, remember
from finengine.skills import run_orchestrator as ro

pytestmark = pytest.mark.usefixtures("coordination")

SUBTASK = "sub-1"


class NoGoal:
    async def __call__(self, record: TurnRecord, emit: object) -> GoalResult:  # pragma: no cover
        raise AssertionError("в этих тестах цель не исполняется")


class AnotherReplica:
    """`turn_gateway` другой реплики: хода у неё нет, корень ей назвать нельзя."""

    def root_known(self, run_id: str, task_id: str) -> bool:
        return False


def turn_script(called: asyncio.Event):
    async def script(connection: ServerConnection, server: FakeOuroboros) -> None:
        sent = json.loads(await connection.recv())
        server.received.append(sent)
        run_id = sent["client_message_id"]
        await connection.send(json.dumps(echo(run_id, sent["content"])))
        await called.wait()
        await connection.send(json.dumps(assistant(run_id, "итог подзадачи", task_id=SUBTASK)))
        await connection.send(json.dumps(assistant(run_id, "ответ корня", task_id=ROOT)))
        await connection.wait_closed()

    return script


async def run_turn_with_a_tool_call(api_of) -> list[str]:
    called = asyncio.Event()
    async with FakeOuroboros(turn_script(called)) as server:
        try:
            gateway = gateway_of(server)
            runner = TurnRunner(gateway)
            runner.run_id = ro.start_run(runner, kind="chat", meta={})
            remember(
                TurnRecord(
                    run_id=runner.run_id, owner_id="7", chat_id="project-1", message="вопрос",
                    user_id=7, case_id="project-1", doc_ids=(), active_doc_id="", role="",
                )
            )
            await server.wait_received(1)
            plugin = Plugin(api_of(gateway))
            reply = await plugin.call("aifa_wait", ctx_of(runner.run_id, task_id=ROOT))
            # Корень называет привязка, ещё до разбора аргументов: аргументов здесь нарочно нет.
            assert first_line(reply) == "ОТКАЗ неверные_аргументы"
            await asyncio.sleep(0.3)  # команда по шине доходит до владельца прогона
        finally:
            called.set()
        events = [e async for e in ro.subscribe(runner.run_id, kind="chat")]
        await plugin.close()
    return [e["text"] for e in events if e["type"] == "text_delta"]


async def test_the_root_named_by_a_tool_call_reaches_the_gateway_of_the_same_replica() -> None:
    texts = await run_turn_with_a_tool_call(lambda gateway: ToolApi(gateway, NoGoal()))

    assert texts == ["ответ корня"], "итог подзадачи не принят за финал: корень известен"


async def test_the_root_reaches_the_gateway_of_another_replica_by_the_control_channel() -> None:
    """P52: вызов инструмента попал не на ту реплику, где живёт ход — корень едет каналом управления."""
    texts = await run_turn_with_a_tool_call(lambda gateway: ToolApi(AnotherReplica(), NoGoal()))

    assert texts == ["ответ корня"]


async def test_without_a_named_root_the_gateway_takes_the_first_reply_for_the_final() -> None:
    """Контрольный: на чём держатся два теста выше — без вызова инструмента финалом становится итог подзадачи."""
    called = asyncio.Event()
    async with FakeOuroboros(turn_script(called)) as server:
        try:
            runner = TurnRunner(gateway_of(server))
            runner.run_id = ro.start_run(runner, kind="chat", meta={})
            await server.wait_received(1)
        finally:
            called.set()
        events = [e async for e in ro.subscribe(runner.run_id, kind="chat")]

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["итог подзадачи"]
