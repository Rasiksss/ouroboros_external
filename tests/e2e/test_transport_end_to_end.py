"""All four transports on one in-process gateway, delivering end to end.

The acceptance check for the seam: the supervisor publishes once per source onto
a single outbox, every transport reads its own subscription, and each delivers
exactly its own messages while acknowledging and dropping the rest. Nothing here
patches a transport's internals — each one is wired the way the composition root
wires it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from a2a.server.agent_execution.context import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import SendMessageRequest
from pydantic import SecretStr

from new_core.identity import OwnerScope
from ouroboros.app.transport_lifecycle import (
    StartedTransports,
    stop_transports_when_supervisor_exits,
)
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.schema import AppSettings
from supervisor.message_bus import send_with_budget
from supervisor.runtime_context import reset_runtime_for_tests
from tests.helpers.a2a import make_event_queue, make_executor
from tests.helpers.dialogs_cursor import sql_dialogs_cursor_repository
from tests.helpers.fake_gateway import RecordingMemoryEvents
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.scope import scope_for
from tests.helpers.shared import AllowAllSkillPermissionChecker
from tests.helpers.supervisor import bootstrap_supervisor_for_tests, make_supervisor_loop
from transport.a2a.delivery import A2AResponseRouter
from transport.a2a.executor import derive_a2a_chat_id
from transport.a2a.proto import new_user_text_message, task_state_name
from transport.browser.ws import BrowserWebSocketHub
from transport.dialogs import build_dialogs_transport
from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox
from transport.interfaces import OutboundMessage
from transport.skill_bridge import SkillBridge

DIALOGS_CHAT = "peer-42"
BROWSER_CHAT = 1


@parametrize_from_dict(
    {
        "retry while running": {"retry": "inflight"},
        "retry after completion": {"retry": "completed"},
        "conflicting text cannot consume the first answer": {"retry": "conflict"},
    }
)
def test_a2a_retry_without_task_id_gets_one_jobs_result(tmp_path, monkeypatch, retry):
    monkeypatch.setenv("OUROBOROS_HARD_TIMEOUT_SEC", "1")
    runtime_persistence.initialize()
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    runtime = bootstrap_supervisor_for_tests(tmp_path, gateway=gateway)
    ctx = SimpleNamespace(**vars(runtime), send_with_budget=send_with_budget)
    supervisor = make_supervisor_loop(SimpleNamespace(ctx=ctx))
    router = A2AResponseRouter(gateway.outbox)
    executor = make_executor(tmp_path, gateway=gateway, router=router)
    answer = "Employee grades: [table](https://example.org/grades)"

    async def scenario():
        calls, contexts, replies = [], [], []
        router.start()

        async def submit(text="List the employee grades"):
            message = new_user_text_message(text)
            message.message_id = "same-message"
            context = RequestContext(
                call_context=ServerCallContext(), request=SendMessageRequest(message=message), context_id="same-context"
            )
            queue, events = make_event_queue()
            contexts.append(context)
            replies.append(events)
            calls.append(asyncio.create_task(executor.execute(context, queue)))
            envelope = await asyncio.to_thread(gateway.inbox.poll, 2)
            assert envelope is not None
            await asyncio.to_thread(supervisor.handle_inbound, envelope)

        try:
            await submit()
            if retry != "completed":
                await submit("Different request" if retry == "conflict" else "List the employee grades")
            if retry == "conflict":
                await calls[1]
                for text in ("Different request", "Another conflicting request"):
                    await submit(text)
                    await calls[-1]
            (original,) = runtime.tasks_repo.list_tasks()
            runtime.tasks_repo.update_task_status(original.task_id, "completed", result=answer)
            send_with_budget(
                answer,
                fmt="markdown",
                scope=OwnerScope.from_dict(original.input["scope"]),
                source="a2a",
                transport=original.input["transport"],
                task_id=original.task_id,
            )
            await calls[0]
            if retry == "completed":
                await submit()
            await asyncio.gather(*calls)
            assert len({context.task_id for context in contexts}) == len(contexts)
            assert [task_state_name(events[-1].status.state) for events in replies] == ["completed"] * len(replies)
            assert answer in str(replies[0])
            assert all(
                ("конфликт идентификатора" if retry == "conflict" else answer) in str(reply) for reply in replies[1:]
            )
            assert len(runtime.tasks_repo.list_tasks()) == 1
            rows = runtime.conversations_repo.list_chat_history_messages(
                scope=OwnerScope.from_dict(original.input["scope"])
            )
            assert [row.content for row in rows if row.actor_type == "owner"] == ["List the employee grades"]
            assert [row.content for row in rows if row.actor_type == "agent" and row.content == answer] == [answer]
            assert len(rows) == (4 if retry == "conflict" else 2)
        finally:
            for call in calls:
                call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            router.stop()

    try:
        asyncio.run(scenario())
    finally:
        reset_runtime_for_tests()


class _DialogsProvider:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    def connect(self) -> None: ...

    def authenticate(self) -> None: ...

    def close(self) -> None: ...

    def recv_update(self, _cursor, timeout_sec=0):
        time.sleep(0.01)
        return None, _cursor

    def can_send_to_chat_id(self, _chat_id) -> bool:
        return True

    def send_text(self, chat_id, text, fmt="", **_kwargs):
        self.sent.append((chat_id, text))
        return True, "ok"


def _dialogs_settings() -> AppSettings:
    return AppSettings(
        DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
        DIALOGS_BOT_TOKEN=SecretStr("token"),
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """One gateway, four transports, exactly as the composition root wires them."""
    runtime_persistence.initialize()
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())

    provider = _DialogsProvider()
    dialogs = build_dialogs_transport(
        settings=_dialogs_settings(),
        gateway=gateway,
        storage=get_storage_provider(),
        drive_root=tmp_path,
        session_id="sess-test",
        provider_factory=lambda _settings: provider,
        cursor_repository=sql_dialogs_cursor_repository(),
    )
    dialogs.start()

    browser_frames: list[dict] = []

    def _record(frame, **_kwargs):
        browser_frames.append(frame)
        from transport.browser.ws import DeliveryOutcome

        return DeliveryOutcome.DELIVERED

    browser = BrowserWebSocketHub()
    monkeypatch.setattr(browser, "clients_for", lambda **_kwargs: [object()])
    monkeypatch.setattr(browser, "broadcast_sync", _record)
    monkeypatch.setattr(browser, "deliver_blocking", _record)
    browser.attach_gateway(gateway, drive_root=tmp_path)

    loop = asyncio.new_event_loop()
    a2a = A2AResponseRouter(gateway.outbox)
    a2a.start()
    a2a_chat = derive_a2a_chat_id("e2e-task")
    pending = a2a.register(scope_for("a2a", a2a_chat), "e2e-task", loop)

    skills = SkillBridge(
        tmp_path,
        gateway=gateway,
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        permission_checker=AllowAllSkillPermissionChecker(),
    )
    monkeypatch.setattr(skills, "require_permission", lambda *_args: None)

    try:
        yield SimpleNamespace(
            gateway=gateway,
            provider=provider,
            browser_frames=browser_frames,
            a2a_chat=a2a_chat,
            pending=pending,
            loop=loop,
            skills=skills,
        )
    finally:
        dialogs.close()
        browser._detach_gateway()
        a2a.stop()
        loop.close()


def _await(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_every_transport_delivers_only_its_own_messages(wired):
    for message in (
        OutboundMessage(scope=scope_for("dialogs", DIALOGS_CHAT), source="dialogs", text="for dialogs"),
        OutboundMessage(scope=scope_for("browser", BROWSER_CHAT), source="browser", text="for browser"),
        OutboundMessage(
            scope=scope_for("a2a", wired.a2a_chat), source="a2a", correlation_id="e2e-task", text="for a2a"
        ),
    ):
        wired.gateway.outbox.publish(message)

    assert _await(lambda: wired.provider.sent)
    assert _await(lambda: any(frame.get("type") == "chat" for frame in wired.browser_frames))
    assert wired.loop.run_until_complete(wired.pending.wait(5)) == "for a2a"

    assert wired.provider.sent == [(DIALOGS_CHAT, "for dialogs")]
    chat_frames = [frame for frame in wired.browser_frames if frame.get("type") == "chat"]
    assert [frame["content"] for frame in chat_frames] == ["for browser"]
    assert wired.pending._text == "for a2a"


def test_a_skill_answer_reaches_the_transport_that_owns_the_chat(wired):
    """The skill bridge publishes; the seam decides who delivers."""
    wired.skills.send_skill_message(
        "distribution:tasks-scheduler",
        "Lunch time",
        chat_id=BROWSER_CHAT,
        source="browser",
    )

    assert _await(lambda: any(frame.get("type") == "chat" for frame in wired.browser_frames))
    assert wired.provider.sent == []
    chat_frames = [frame for frame in wired.browser_frames if frame.get("type") == "chat"]
    assert [frame["content"] for frame in chat_frames] == ["Lunch time"]


def test_every_transport_submits_onto_the_one_inbox(wired, tmp_path):
    """Inbound converges: four producers, one queue, one envelope shape."""
    from transport.dialogs.messages import InboundMessage

    dialogs = build_dialogs_transport(
        settings=_dialogs_settings(),
        gateway=wired.gateway,
        storage=get_storage_provider(),
        drive_root=tmp_path,
        session_id="sess-test",
        provider_factory=lambda _settings: wired.provider,
        cursor_repository=sql_dialogs_cursor_repository(),
    )
    dialogs.handle_inbound(
        InboundMessage(chat_id=DIALOGS_CHAT, user_id="user-42", text="from dialogs", source_message_id="mid-e2e")
    )
    wired.skills.enqueue_skill_message("distribution:tasks-scheduler", "from a skill", chat_id=BROWSER_CHAT)

    submitted = []
    while True:
        envelope = wired.gateway.inbox.poll()
        if envelope is None:
            break
        submitted.append(envelope)

    assert [envelope.text for envelope in submitted] == ["from dialogs", "from a skill"]
    assert [envelope.source for envelope in submitted] == ["dialogs", "skill-bridge"]


def _dialogs_transport(gateway, drive_root, provider):
    """Constructed the way the composition root does: subscription first, threads later."""
    return build_dialogs_transport(
        settings=_dialogs_settings(),
        gateway=gateway,
        storage=get_storage_provider(),
        drive_root=drive_root,
        session_id="sess-test",
        provider_factory=lambda _settings: provider,
        cursor_repository=sql_dialogs_cursor_repository(),
    )


def test_a_message_published_before_dialogs_starts_is_delivered_once_it_does(tmp_path):
    """The window between the supervisor's start and the transport's used to swallow messages."""
    runtime_persistence.initialize()
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    provider = _DialogsProvider()
    dialogs = _dialogs_transport(gateway, tmp_path, provider)
    try:
        gateway.outbox.publish(
            OutboundMessage(scope=scope_for("dialogs", DIALOGS_CHAT), source="dialogs", text="published early")
        )
        dialogs.start()

        assert _await(lambda: provider.sent)
        assert provider.sent == [(DIALOGS_CHAT, "published early")]
    finally:
        dialogs.close()


def test_the_supervisors_exit_stops_the_transports_it_started(tmp_path, caplog):
    """A supervisor that returned drains nothing; a transport left running accepts forever."""
    runtime_persistence.initialize()
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    dialogs = _dialogs_transport(gateway, tmp_path, _DialogsProvider())
    dialogs.start()
    transports = StartedTransports()
    transports.register("dialogs", dialogs.stop)

    with caplog.at_level(logging.CRITICAL):
        stop_transports_when_supervisor_exits(
            lambda: None,
            transports=transports,
            supervisor_error=lambda: "Supervisor loop stopped: too many crashes",
        )

    assert dialogs.closed
    assert [record.message for record in caplog.records if record.levelno == logging.CRITICAL]


def test_a_transport_that_fails_to_stop_does_not_keep_the_others_running(tmp_path):
    runtime_persistence.initialize()
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    dialogs = _dialogs_transport(gateway, tmp_path, _DialogsProvider())
    dialogs.start()
    transports = StartedTransports()
    transports.register("browser", lambda: (_ for _ in ()).throw(RuntimeError("hub is gone")))
    transports.register("dialogs", dialogs.stop)

    transports.stop_all()

    assert dialogs.closed


def test_the_agent_cards_skill_catalog_is_a_live_view(monkeypatch):
    """A card built from a startup snapshot advertises what the process no longer runs."""
    from new_core.tools import catalog as catalog_module
    from ouroboros.app.transport_wiring import CoreToolSkillCatalog
    from ouroboros.policy.capabilities import CORE_TOOL_NAMES

    first, second = sorted(CORE_TOOL_NAMES)[:2]

    def _catalog(*names):
        descriptors = [SimpleNamespace(name=name, schema=SimpleNamespace(description=name)) for name in names]
        return SimpleNamespace(providers=("builtin",) if names else (), descriptors=lambda _scope: descriptors)

    catalog = CoreToolSkillCatalog()

    monkeypatch.setattr(catalog_module, "tool_catalog", _catalog())
    # Empty means the lifespan has not wired it yet; the next request must ask again.
    assert catalog.skills() == []

    monkeypatch.setattr(catalog_module, "tool_catalog", _catalog(first))
    assert [spec.id for spec in catalog.skills()] == [first]

    monkeypatch.setattr(catalog_module, "tool_catalog", _catalog(first, second))
    assert [spec.id for spec in catalog.skills()] == [first, second]


def test_reading_the_session_id_costs_no_usage_aggregate():
    """``load`` sums the whole llm_usage table; the root reads the id, not the state."""
    import server

    calls: list[str] = []
    repository = SimpleNamespace(
        session_id=lambda: (calls.append("session_id"), "sess-42")[1],
        load=lambda: calls.append("load"),
    )

    assert server._chat_session_id(repository) == "sess-42"
    assert calls == ["session_id"]
