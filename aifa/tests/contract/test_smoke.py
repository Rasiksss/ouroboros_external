"""Дымовой контрактный тест: ход «привет» через границу служба ↔ ouroboros (шаги 07, 08).

Настоящий ouroboros профиля `contract`, подставная модель отвечает по `scenarios/hello.json`; ход
ведёт настоящий `turn_gateway`. Поля кадров (C2.2, C2.3) проверяет `test_turn_contract.py`.
"""

from __future__ import annotations

import pytest

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin
from turn_support import collect, gateway_of, new_chat, new_request, screen

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

HELLO_ANSWER = "Здравствуйте! Это подставная модель."


async def test_hello_gets_the_scripted_answer_and_the_model_sees_the_text_verbatim(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """Ответ сценария доходит до журнала хода; реплика уходит к модели дословно (F6)."""
    stub.use_scenario(SCENARIOS / "hello.json")

    events = await collect(gateway_of(stand), new_request("привет"))

    assert [e["type"] for e in screen(events)] == ["text_delta", "text_segment_end", "done"]
    assert screen(events)[0]["text"] == HELLO_ANSWER
    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    assert [e["user"] for e in asked] == ["привет"], "модель увидела реплику дословно, без добавок"
    assert stub.unmatched() == [], "на каждый запрос к модели в сценарии есть правило"


async def test_hello_twice_gives_the_same_answer_and_each_turn_takes_its_own_final(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """Повтор в том же чате — тот же ответ; ход берёт финал своего `run_id`, а не предыдущего."""
    stub.use_scenario(SCENARIOS / "hello.json")
    chat_id = new_chat()
    gateway = gateway_of(stand)

    first = await collect(gateway, new_request("привет", chat_id=chat_id))
    second = await collect(gateway, new_request("привет", chat_id=chat_id))

    assert screen(first)[0]["text"] == screen(second)[0]["text"] == HELLO_ANSWER
    assert [e["type"] for e in screen(second)] == ["text_delta", "text_segment_end", "done"]
