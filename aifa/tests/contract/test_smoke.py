"""Дымовой контрактный тест: ход «привет» через границу служба ↔ ouroboros (шаг 07).

Настоящий ouroboros профиля `contract`, подставная модель отвечает по `scenarios/hello.json`.
Закрывает C2.2 (эхо) и C2.3 (финальный кадр корня) по полям CONTRACTS.md §2.3.
"""

from __future__ import annotations

import uuid

import pytest

from ouro_ws import Turn, run_turn
from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin

pytestmark = pytest.mark.contract

HELLO_ANSWER = "Здравствуйте! Это подставная модель."


def _hello(stand: ContractStand, chat_id: str) -> Turn:
    return run_turn(
        stand.ouro_ws_url,
        owner_id="contract-owner",
        chat_id=chat_id,
        text="привет",
        run_id=uuid.uuid4().hex,
    )


def test_hello_gets_the_scripted_answer_with_echo_and_final(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.2 + C2.3: эхо несёт `run_id`, финал — `task_id` и `transport.source_message_id`."""
    stub.use_scenario(SCENARIOS / "hello.json")

    turn = _hello(stand, f"smoke-{uuid.uuid4().hex[:8]}")

    echo, final = turn.echo, turn.final
    assert echo is not None and final is not None
    assert echo["content"] == "привет"
    assert echo["client_message_id"] == turn.run_id
    assert final["content"] == HELLO_ANSWER
    assert final["is_progress"] is False
    assert final["task_id"]
    assert final["transport"]["source_message_id"] == turn.run_id
    assert turn.frames.index(echo) < turn.frames.index(final)

    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    assert [e["user"] for e in asked] == ["привет"], "модель увидела реплику дословно, без добавок"
    assert stub.unmatched() == [], "на каждый запрос к модели в сценарии есть правило"


def test_hello_twice_gives_the_same_answer_and_each_final_names_its_own_run(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """Повтор в том же чате — тот же ответ; финал принадлежит своему ходу, а не предыдущему."""
    stub.use_scenario(SCENARIOS / "hello.json")
    chat_id = f"smoke-{uuid.uuid4().hex[:8]}"

    first, second = _hello(stand, chat_id), _hello(stand, chat_id)

    assert first.final is not None and second.final is not None
    assert first.final["content"] == second.final["content"] == HELLO_ANSWER
    assert first.run_id != second.run_id
    assert second.final["transport"]["source_message_id"] == second.run_id
