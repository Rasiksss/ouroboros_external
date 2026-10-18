"""Ход с вызовом инструмента через настоящий ouroboros: круг и результат доходят до сценария.

Б1 ревью 07: после круга с инструментом О дописывает `user`-записку; сценарий не должен принимать
её за новую реплику (иначе круг вечно 1 и ход упирается в MAX_ROUNDS). `scenarios/tool_round.json`.
"""

from __future__ import annotations

import uuid

import pytest

from ouro_ws import run_turn
from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin

pytestmark = pytest.mark.contract

REQUEST = "покажи инструменты"
ANSWER = "Список инструментов получен."


def test_a_tool_call_is_answered_in_the_second_round_with_its_result_visible(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """Круг 1 — вызов инструмента, круг 2 видит его результат и ту же реплику; ход завершён."""
    stub.use_scenario(SCENARIOS / "tool_round.json")

    turn = run_turn(
        stand.ouro_ws_url,
        owner_id="contract-owner",
        chat_id=f"tool-{uuid.uuid4().hex[:8]}",
        text=REQUEST,
        run_id=uuid.uuid4().hex,
    )

    assert turn.final is not None and turn.final["content"] == ANSWER
    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    assert [(e["round"], e["rule"]) for e in asked] == [
        (1, "call-tool"),
        (2, "answer-after-tool"),
    ]
    assert asked[1]["user"] == REQUEST
    tool_messages = [m for m in asked[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and asked[1]["last_tool_result"] == tool_messages[-1]["content"]
    assert stub.unmatched() == [], "на каждый запрос к модели в сценарии есть правило"
