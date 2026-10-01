from __future__ import annotations

from types import SimpleNamespace


def _command_context(agent):
    sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
    busy_task_id = "active-task" if agent is not None else None
    ctx = SimpleNamespace(
        executor=SimpleNamespace(get_slot=lambda _worker_id: SimpleNamespace(busy_task_id=busy_task_id)),
        orchestrator=SimpleNamespace(_agent_getter=lambda: agent),
        send_with_budget=lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    return ctx, sent


def test_stop_injects_owner_instruction_once_without_canned_response() -> None:
    from supervisor.commands import handle_slash_command

    injected: list[str] = []
    agent = SimpleNamespace(_busy=True, inject_message=injected.append)
    ctx, sent = _command_context(agent)

    handled = handle_slash_command("/stop", chat_id=42, ctx=ctx)

    assert handled is True
    assert len(injected) == 1
    instruction = injected[0]
    assert instruction.isascii()
    assert len(instruction) < 500
    assert "priority instruction" in instruction
    assert "`recent_tasks`" in instruction
    assert "`cancel_task`" in instruction
    assert "current foreground task" in instruction
    assert "only requested" in instruction
    assert sent == []


def test_stop_without_active_foreground_flow_preserves_reply_route() -> None:
    from supervisor.commands import handle_slash_command

    ctx, sent = _command_context(None)

    def unexpected_agent_getter():
        raise AssertionError("idle /stop must not create an agent")

    ctx.orchestrator._agent_getter = unexpected_agent_getter
    transport = {"target_uid": "owner-7", "reply_to": "message-9"}

    handled = handle_slash_command(
        " /stop ",
        chat_id=42,
        ctx=ctx,
        source="dialogs",
        transport=transport,
    )

    assert handled is True
    assert sent == [
        (
            (42, "Активной задачи нет."),
            {"source": "dialogs", "transport": transport},
        )
    ]
