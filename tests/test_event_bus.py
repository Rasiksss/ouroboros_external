import asyncio
from types import SimpleNamespace

from ouroboros.observability.event_bus import CHAT_OUTBOUND, SKILL_LIFECYCLE, EventBus


def test_event_bus_publishes_to_matching_topic() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", CHAT_OUTBOUND, received.append)
    bus.publish(CHAT_OUTBOUND, {"text": "hello"})

    assert received == [{"text": "hello", "topic": CHAT_OUTBOUND}]


def test_event_bus_supports_skill_lifecycle_topic() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", SKILL_LIFECYCLE, received.append)
    bus.publish(SKILL_LIFECYCLE, {"type": "skill_exec_finished", "skill": "demo"})

    assert received == [
        {
            "type": "skill_exec_finished",
            "skill": "demo",
            "topic": SKILL_LIFECYCLE,
        }
    ]


def test_event_bus_unsubscribe_skill_removes_handlers() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", CHAT_OUTBOUND, received.append)
    bus.unsubscribe_skill("skill")
    bus.publish(CHAT_OUTBOUND, {"text": "hello"})

    assert received == []


def test_event_bus_rejects_unknown_topic() -> None:
    bus = EventBus()

    try:
        bus.subscribe("skill", "unknown.topic", lambda _payload: None)
    except ValueError as exc:
        assert "unsupported event topic" in str(exc)
    else:
        raise AssertionError("expected unknown topic to be rejected")


def test_event_bus_schedules_async_handler_from_sync_publish() -> None:
    async def main():
        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())
        received = []

        async def handler(payload):
            received.append(payload["text"])

        bus.subscribe("skill", CHAT_OUTBOUND, handler)
        bus.publish(CHAT_OUTBOUND, {"text": "hello"})
        await asyncio.sleep(0.05)
        assert received == ["hello"]

    asyncio.run(main())


def test_supervisor_dispatches_skill_lifecycle_to_log_and_event_bus(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store
    from ouroboros.utils import append_jsonl
    from supervisor.events import dispatch_event

    init_runtime_store(tmp_path)
    published = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        append_jsonl=append_jsonl,
        bridge=SimpleNamespace(),
    )
    monkeypatch.setattr(
        "ouroboros.observability.event_bus.publish_event", lambda topic, payload: published.append((topic, payload))
    )

    dispatch_event({"type": "skill_exec_finished", "skill": "demo", "exit_code": 0}, ctx)

    store = get_runtime_store(tmp_path)
    assert store is not None
    records = [
        event.metadata
        for event in store.list_audit_events(log_stream="events", event_type="skill_exec_finished", limit=1)
    ]
    assert records[-1]["type"] == "skill_exec_finished"
    assert not (tmp_path / "logs" / "events.jsonl").exists()
    assert published[-1][0] == SKILL_LIFECYCLE
    assert published[-1][1]["skill"] == "demo"
