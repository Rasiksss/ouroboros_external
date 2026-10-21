"""Файл, который агент отправил сам (`file_read`), доходит до экрана событием `file` (ADR-0059).

Dev, 21.10.2026: агент собрал xlsx в песочнице и отправил; ouroboros положил его в каталог и прислал
кадр с `attachments`, а экран не получил ничего. Шлюз хода — подставной (его кадры проверяет
`test_assistant_turn_gateway.py`), карточка источника — подставная (её проверяет
`test_files_live_in_the_catalog.py`).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from finengine.assistant.chat_turn import OuroTurn
from finengine.assistant.events import service_event
from finengine.skills import sources

ATTACHMENT = service_event(
    "attachment", catalog_file_id="f1", filename="итоги.xlsx", size=6946,
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)


class Gateway:
    def __init__(self, *events: dict[str, Any]) -> None:
        self._events = events

    async def events(self, request: Any, *, on_root: Any = None) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event


def turn_of(gateway: Gateway, project_id: str = "p1") -> OuroTurn:
    return OuroTurn(
        gateway=gateway, run={"run_id": "run-1"}, owner_id="7", chat_id=project_id or "s1", session_id="s1",  # type: ignore[arg-type]
        trace_id="t", message="сделай excel", user_id=7, username="u", project_id=project_id, doc_ids=[],
    )


@pytest.fixture(autouse=True)
def quiet_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("finengine.skills.run_logger.log_chat_turn", lambda **_: None)


async def collect(turn: OuroTurn) -> list[dict[str, Any]]:
    return [event async for event in turn()]


async def test_an_attachment_of_the_agent_is_a_file_button_of_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    adopted: list[tuple[Any, ...]] = []

    def adopt(user_id: int, project_id: str, catalog_file_id: str, filename: str, mime: str, size: int) -> Any:
        adopted.append((user_id, project_id, catalog_file_id, filename, mime, size))
        return SimpleNamespace(file_id="src-1", filename=filename)

    monkeypatch.setattr(sources, "adopt_catalog_file", adopt)

    events = await collect(turn_of(Gateway(ATTACHMENT, {"type": "text_delta", "text": "Готово."})))

    assert adopted == [(7, "p1", "f1", "итоги.xlsx", ATTACHMENT["mime"], 6946)]
    assert {"type": "file", "file_id": "src-1", "filename": "итоги.xlsx", "kind": "xlsx"} in events
    assert events.index(ATTACHMENT) < events.index({"type": "text_delta", "text": "Готово."})


async def test_a_file_that_cannot_be_adopted_does_not_break_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any) -> Any:
        raise ValueError("extension '.exe' not allowed: executables are not delivered")

    monkeypatch.setattr(sources, "adopt_catalog_file", refuse)

    events = await collect(turn_of(Gateway(ATTACHMENT, {"type": "text_delta", "text": "Готово."})))

    assert not any(event["type"] == "file" for event in events)
    assert {"type": "text_delta", "text": "Готово."} in events
