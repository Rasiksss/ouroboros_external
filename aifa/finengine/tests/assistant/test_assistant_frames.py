"""Классификация кадров ouroboros (CONTRACTS.md §2.3, §2.4)."""
from __future__ import annotations

import pytest
from fake_ouroboros import assistant, echo

from finengine.assistant.frames import FrameKind, classify

RUN = "run-1"
ROOT = "r00t"


@pytest.mark.parametrize(
    ("frame", "root", "expected"),
    [
        (echo(RUN, "вопрос"), "", FrameKind.ECHO),
        (echo("other-run", "вопрос"), "", FrameKind.FOREIGN),
        (assistant(RUN, "💬 думаю", task_id=ROOT, progress=True), ROOT, FrameKind.PROGRESS),
        (assistant(RUN, "ответ", task_id=ROOT), ROOT, FrameKind.FINAL),
        (assistant(RUN, "итог", task_id="sub1"), ROOT, FrameKind.SUBTASK_RESULT),
        (assistant(RUN, "🗓️ Scheduled task sub1: …"), ROOT, FrameKind.NOTIFICATION),
        (assistant(RUN, "ответ", task_id=ROOT, source="another-run"), ROOT, FrameKind.FOREIGN),
        (assistant(RUN, "итог", task_id="sub1", source="another-run"), ROOT, FrameKind.FOREIGN),
        (assistant(RUN, "🛑 Остановка запрошена", source=f"{RUN}-stop"), ROOT, FrameKind.FOREIGN),
        ({"type": "typing", "action": "typing"}, ROOT, FrameKind.TYPING),
        ({"type": "heartbeat"}, ROOT, FrameKind.HEARTBEAT),
        ({"type": "error", "code": "message_too_long"}, ROOT, FrameKind.ERROR),
        ({"type": "confirmation_request", "tool": "x"}, ROOT, FrameKind.CONFIRMATION),
        ({"type": "log", "data": {}}, ROOT, FrameKind.UNEXPECTED),
        ({"type": "extension_lifecycle"}, ROOT, FrameKind.UNEXPECTED),
        ({"type": "photo", "role": "assistant"}, ROOT, FrameKind.UNEXPECTED),
        ({"type": "unreadable"}, ROOT, FrameKind.UNEXPECTED),
    ],
)
def test_a_frame_means_what_the_table_says(
    frame: dict[str, object], root: str, expected: FrameKind
) -> None:
    """§2.3: вид кадра по типу, роли, `is_progress`, `task_id` и `transport.source_message_id`."""
    assert classify(frame, run_id=RUN, root_task_id=root) is expected


def test_without_a_known_root_the_first_non_progress_task_frame_is_the_final() -> None:
    """§2.4: корень не назван вызовом инструмента — им становится первый непрогрессный кадр с `task_id`."""
    frame = assistant(RUN, "ответ", task_id="who-knows")

    assert classify(frame, run_id=RUN, root_task_id="") is FrameKind.FINAL


def test_once_the_root_is_named_the_same_frame_is_a_subtask_result() -> None:
    """§2.4: корень назван (`root_known`) — кадр с другим `task_id` уже итог подзадачи, а не финал."""
    frame = assistant(RUN, "итог", task_id="sub1")

    assert classify(frame, run_id=RUN, root_task_id=ROOT) is FrameKind.SUBTASK_RESULT
    assert classify(frame, run_id=RUN, root_task_id="") is FrameKind.FINAL


def test_a_task_frame_without_the_run_in_its_transport_is_unexpected_not_final() -> None:
    """У непрогрессного кадра с `task_id` нет `source_message_id` — финалом его не считаем."""
    frame = assistant(RUN, "ответ", task_id=ROOT)
    del frame["transport"]

    assert classify(frame, run_id=RUN, root_task_id=ROOT) is FrameKind.UNEXPECTED
