"""Ход с вызовом инструмента через настоящий ouroboros: круг и результат доходят до сценария.

Б1 ревью 07: после круга с инструментом О дописывает `user`-записку; сценарий не должен принимать
её за новую реплику (иначе круг вечно 1 и ход упирается в MAX_ROUNDS). `scenarios/tool_round.json`.
"""

from __future__ import annotations

import pytest

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin
from turn_support import collect, gateway_of, new_request, screen

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

REQUEST = "покажи инструменты"
ANSWER = "Список инструментов получен."


async def test_a_tool_call_is_answered_in_the_second_round_with_its_result_visible(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """Круг 1 — вызов инструмента, круг 2 видит его результат и ту же реплику; ход завершён."""
    stub.use_scenario(SCENARIOS / "tool_round.json")

    events = await collect(gateway_of(stand), new_request(REQUEST))

    assert screen(events)[0] == {"type": "text_delta", "text": ANSWER}
    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    assert [(e["round"], e["rule"]) for e in asked] == [
        (1, "call-tool"),
        (2, "answer-after-tool"),
    ]
    assert asked[1]["user"] == REQUEST
    tool_messages = [m for m in asked[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and asked[1]["last_tool_result"] == tool_messages[-1]["content"]
    assert stub.unmatched() == [], "на каждый запрос к модели в сценарии есть правило"
