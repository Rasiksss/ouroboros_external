import pytest

import ouroboros.observability.event_bus as event_bus
import supervisor.message_bus as message_bus
from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import StoredFile
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.fake_gateway import RecordingOutbox
from tests.helpers.files import stored_file
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE, scope_with

#: The conversation the outbound-kind cases address.
_CHAT_42 = OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="42")
#: The operation the media cases are published under.
_TRACE_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_media_and_typing_carry_their_source_kind_and_operation_across_the_outbox(tmp_path):
    from ouroboros.observability import aef_tracing
    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests
    from transport.interfaces import OutboundKind, SubscriptionMode

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    bridge = runtime.bridge
    reader = runtime.gateway.outbox.reader(name="test", mode=SubscriptionMode.BROADCAST)
    published = []
    try:
        with aef_tracing.trace_scope(_TRACE_ID):
            bridge.send_image(
                StoredFile(stored_file(scope=_CHAT_42, filename="image.png", mime="image/png")["id"]),
                mime="image/png",
                source="dialogs",
                scope=_CHAT_42,
            )
            bridge.send_file(
                StoredFile(stored_file(scope=_CHAT_42)["id"]), filename="report.txt", source="dialogs", scope=_CHAT_42
            )
            for source in ("dialogs", "browser"):
                bridge.send_chat_action(scope_for(source, 1), source=source)
        for _ in range(4):
            delivered = reader.poll(1.0)
            assert delivered is not None
            published.append(delivered.message)
            reader.ack(delivered)
    finally:
        reader.close()
        reset_runtime_for_tests()
    assert [m.kind for m in published] == [
        OutboundKind.PHOTO,
        OutboundKind.FILE,
        OutboundKind.ACTION,
        OutboundKind.ACTION,
    ]
    assert [m.trace_id for m in published[:2]] == [_TRACE_ID, _TRACE_ID]
    assert [m.source for m in published[2:]] == ["dialogs", "browser"]


def test_send_photo_publishes_transport_event_with_reference(monkeypatch, tmp_path):
    bridge = message_bus.LocalChatBridge(RecordingOutbox(), tmp_path)
    media = StoredFile(stored_file(filename="image.png", mime="image/png")["id"])
    events = []
    monkeypatch.setattr(event_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, _ = bridge.send_image(media, caption="caption", mime="image/png", source="browser", scope=TEST_SCOPE)

    assert ok is True
    topic, payload = events[-1]
    assert topic == event_bus.CHAT_PHOTO
    assert payload["attachment"] == {"id": media.file_id}
    assert payload["image_base64"] == ""
    assert payload["caption"] == "caption"
    assert payload["mime"] == "image/png"
    assert payload["source"] == "browser"


def test_the_bridge_refuses_a_message_with_no_source(monkeypatch):
    """A source-less message reaches no transport, so it never gets published."""
    bridge = message_bus.LocalChatBridge(RecordingOutbox())

    for send in (
        lambda: bridge.send_message("reply", scope=TEST_SCOPE),
        lambda: bridge.send_chat_action(TEST_SCOPE),
        lambda: bridge.send_image(StoredFile("unused-file"), mime="image/png", scope=TEST_SCOPE),
        lambda: bridge.send_file(StoredFile("unused-file"), filename="a.txt", scope=TEST_SCOPE),
    ):
        with pytest.raises(ValueError, match="requires an explicit source"):
            send()


@parametrize_from_dict(
    {
        "text": {
            "send": lambda bridge: message_bus.send_with_budget(
                "ответ", scope=_CHAT_42, source="dialogs", task_id="task-1"
            )
        },
        "progress": {
            "send": lambda bridge: message_bus.send_with_budget(
                "работаю", scope=_CHAT_42, source="dialogs", task_id="task-1", is_progress=True
            )
        },
        "runtime-notice": {
            "send": lambda bridge: message_bus.send_with_budget("статус: ок", scope=_CHAT_42, source="dialogs")
        },
        "image": {
            "send": lambda bridge: bridge.send_image(
                StoredFile(stored_file(scope=_CHAT_42, filename="image.png", mime="image/png")["id"]),
                caption="картинка",
                source="dialogs",
                scope=_CHAT_42,
            )
        },
        "file": {
            "send": lambda bridge: bridge.send_file(
                StoredFile(stored_file(scope=_CHAT_42)["id"]),
                filename="report.txt",
                caption="файл",
                source="dialogs",
                scope=_CHAT_42,
            )
        },
        "legacy-sberchat": {
            "send": lambda bridge: message_bus.send_with_budget(
                "ответ", scope=_CHAT_42, source="sberchat", task_id="task-1"
            )
        },
    }
)
def test_every_outbound_kind_is_stored_under_the_owners_axes(tmp_path, send):
    from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests

    bridge = bootstrap_supervisor_for_tests(tmp_path).bridge

    try:
        append_chat_message(
            tmp_path,
            scope=scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="42"),
            actor_type="owner",
            chat_id="42",
            user_id="7",
            text="сделай",
            session_id="session-1",
            source="dialogs",
        )
        send(bridge)
    finally:
        reset_runtime_for_tests()

    stored = [
        (message.actor_type, message.owner_id, message.agent_id)
        for message in list_owner_messages(tmp_path, include_progress=True, scope=_CHAT_42)
    ]
    owner, agent = TEST_SCOPE.owner_id, TEST_SCOPE.agent_id
    assert stored == [("owner", owner, agent), ("agent", owner, agent)]


def test_transportless_progress_is_still_owned(tmp_path):
    """A source no surface claims is service work, not a missing owner."""
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests

    bootstrap_supervisor_for_tests(tmp_path)
    try:
        message_bus.send_with_budget(
            "Skill install: `demo` — running",
            scope=scope_with(TEST_SCOPE, surface_id="skill-bridge", chat_id="0"),
            is_progress=True,
            task_id="skill_lifecycle_install_demo",
            source="internal",
        )
    finally:
        reset_runtime_for_tests()

    stored = list_owner_messages(
        tmp_path, include_progress=True, scope=scope_with(TEST_SCOPE, surface_id="skill-bridge", chat_id="0")
    )
    assert [(message.owner_id, message.agent_id) for message in stored] == [(TEST_SCOPE.owner_id, TEST_SCOPE.agent_id)]
    assert not hasattr(stored[0], "conversation_id")


def test_send_with_budget_preserves_routing_and_request_correlation(tmp_path):
    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests
    from transport.interfaces import SubscriptionMode

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    state = runtime.state_repo.load()
    state.owner_id = "user-42"
    state.owner_scope = scope_with(TEST_SCOPE, surface_id="browser", chat_id="peer-42").to_dict()
    runtime.state_repo.save(state)
    reader = runtime.gateway.outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)

    try:
        message_bus.send_with_budget(
            "string id reply",
            scope=scope_with(TEST_SCOPE, surface_id="browser", chat_id="peer-42"),
            source="browser",
            task_id="internal-job",
            transport={"correlation_id": "peer-task"},
        )
        delivered = reader.poll(1.0)
    finally:
        reader.close()
        reset_runtime_for_tests()

    assert delivered is not None
    assert delivered.message.chat_id == "peer-42"
    assert delivered.message.text == "string id reply"
    assert delivered.message.task_id == "internal-job"
    assert delivered.message.correlation_id == "peer-task"


@parametrize_from_dict(
    {
        "history": {"failure": "history"},
        "outbox": {"failure": "outbox"},
        "concurrent": {"failure": "concurrent"},
        "expired": {"failure": "expired"},
        "before": {"failure": "before"},
        "after": {"failure": "after"},
        "retry before receipt confirmation": {"failure": "repeat"},
    }
)
def test_deterministic_send_resumes_pending_handoff_and_preserves_the_receipt(tmp_path, monkeypatch, failure):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests
    from transport.interfaces import SubscriptionMode

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    reader = runtime.gateway.outbox.reader(name="test", mode=SubscriptionMode.BROADCAST)
    publish, entered, release = runtime.gateway.outbox.publish, Event(), Event()
    crash = failure in ("before", "after")

    def send():
        return message_bus.send_with_budget(
            "receipt",
            scope=TEST_SCOPE,
            source="browser",
            client_message_id="admission:job",
            repeat_accepted=failure == "repeat",
        )

    def interrupted(message, *, before_publish=None):
        if failure == "after":
            publish(message, before_publish=before_publish)
        raise SystemExit("process stopped")

    try:
        with monkeypatch.context() as patch:
            if crash:
                patch.setattr(runtime.gateway.outbox, "publish", interrupted)
                with pytest.raises(SystemExit):
                    send()
            else:
                patch.setattr(
                    message_bus if failure == "history" else runtime.gateway.outbox,
                    "append_chat_message" if failure == "history" else "publish",
                    lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable")),
                )
                assert not send() and reader.poll(0) is None
        if crash:
            row = sql_conversation_repository(runtime_persistence.current.engine).list_messages(
                scope=TEST_SCOPE, actor_type="agent"
            )[0]
            assert not send()  # A live ambiguous claim is not stolen.
            sql_conversation_repository(runtime_persistence.current.engine).update_message_metadata(
                row.message_id, {**row.metadata, "handoff_until": 0}
            )
        if failure in ("concurrent", "expired", "repeat"):

            def held_publish(message, *, before_publish=None):
                first = not entered.is_set()
                if first:
                    if failure == "repeat":
                        publish(message, before_publish=before_publish)
                    entered.set()
                    assert release.wait(2)
                if not first or failure != "repeat":
                    publish(message, before_publish=before_publish)

            monkeypatch.setattr(runtime.gateway.outbox, "publish", held_publish)
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(send)
                try:
                    assert entered.wait(2)
                    if failure == "expired":
                        row = sql_conversation_repository(runtime_persistence.current.engine).list_messages(
                            scope=TEST_SCOPE, actor_type="agent"
                        )[0]
                        sql_conversation_repository(runtime_persistence.current.engine).update_message_metadata(
                            row.message_id, {**row.metadata, "handoff_until": 0}
                        )
                    assert pool.submit(send).result(timeout=1) is (failure in ("expired", "repeat"))
                finally:
                    release.set()
                assert first.result() is (failure == "concurrent")
        else:
            assert send()
        if failure != "repeat":
            assert send()  # Durably accepted handoffs do not send again without a new request.
        if crash:
            assert not sql_conversation_repository(runtime_persistence.current.engine).update_message_metadata(
                row.message_id, row.metadata, expected_metadata=row.metadata
            )
        row = sql_conversation_repository(runtime_persistence.current.engine).list_messages(
            scope=TEST_SCOPE, actor_type="agent"
        )[0]
        assert row.metadata["handoff_scope"] == TEST_SCOPE.to_dict()
        for _ in range(2 if failure in ("after", "repeat") else 1):
            delivered = reader.poll(0)
            assert delivered and delivered.message.message_id == row.message_id
            reader.ack(delivered)
        assert reader.poll(0) is None and row.content == "receipt"
        assert (
            len(
                sql_conversation_repository(runtime_persistence.current.engine).list_messages(
                    scope=TEST_SCOPE, actor_type="agent"
                )
            )
            == 1
        )
    finally:
        reader.close()
        reset_runtime_for_tests()


def test_send_with_budget_refuses_an_empty_source():
    """The publish boundary rejects a message with no source instead of defaulting one."""
    import pytest

    with pytest.raises(ValueError, match="explicit source"):
        message_bus.send_with_budget("unrouted reply", scope=TEST_SCOPE, source="")


def test_send_with_budget_requires_source_keyword():
    import pytest

    with pytest.raises(TypeError):
        message_bus.send_with_budget("unrouted reply", scope=TEST_SCOPE)


def test_the_outbound_door_takes_no_chat_id_beside_the_scope():
    """One fact, one parameter — the same rule ``InboundEnvelope`` holds on the way in.

    A chat id next to a scope is the conversation named twice, and the two can drift: the
    timeout notice used to take its route from a task row's column and its identity from the
    scope stored on the same row, so a record whose column was empty was filed under the
    task's conversation and delivered to the owner's.
    """
    import inspect

    doors = [
        message_bus.send_with_budget,
        message_bus.log_chat,
        message_bus.LocalChatBridge.send_message,
        message_bus.LocalChatBridge.send_image,
        message_bus.LocalChatBridge.send_file,
    ]

    for door in doors:
        parameters = inspect.signature(door).parameters
        assert "chat_id" not in parameters, f"{door.__qualname__} names a chat beside its scope"
        scope = parameters["scope"]
        assert scope.kind is inspect.Parameter.KEYWORD_ONLY
        assert scope.default is inspect.Parameter.empty, f"{door.__qualname__} lets its scope default"


# -- a publish that failed is reported as a failure -----------------------------


class _RefusingOutbox(RecordingOutbox):
    """An outbox whose append fails, the way a broker that is briefly gone would."""

    def publish(self, message, *, before_publish=None):
        raise RuntimeError("the outbox is not taking messages")


def test_a_text_message_the_outbox_refused_is_reported_as_a_failure(monkeypatch):
    """Publishing is all-or-nothing, so a refusal is a message no transport will ever see."""
    bridge = message_bus.LocalChatBridge(_RefusingOutbox())
    events: list[tuple] = []
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, reason = bridge.send_message("the answer", source="dialogs", scope=_CHAT_42)

    assert ok is False
    assert reason
    assert events == [], "an outbound event is read as evidence the message was sent"


def test_a_refused_typing_action_is_reported_as_a_failure(monkeypatch):
    bridge = message_bus.LocalChatBridge(_RefusingOutbox())
    events: list[tuple] = []
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    assert bridge.send_chat_action(_CHAT_42, source="dialogs") is False
    assert events == []


def test_a_refused_publish_is_logged_at_error_level(caplog):
    import logging

    bridge = message_bus.LocalChatBridge(_RefusingOutbox())

    with caplog.at_level(logging.ERROR, logger="supervisor.message_bus"):
        bridge.send_message("the answer", source="dialogs", scope=_CHAT_42)

    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_a_refused_publish_leaves_the_history_row_in_place(tmp_path, monkeypatch):
    """The agent did say it, and the next turn's context reads that row."""
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    try:
        monkeypatch.setattr(
            runtime.bridge._outbox,
            "publish",
            lambda message: (_ for _ in ()).throw(RuntimeError("gone")),
        )

        message_bus.send_with_budget("the answer", scope=scope_for("dialogs", 1), source="dialogs")

        contents = [row.content for row in list_owner_messages(tmp_path, scope=scope_for("dialogs", 1))]
        assert "the answer" in contents
    finally:
        reset_runtime_for_tests()


# -- the three chat.* topics are a granted skill capability, gated on the policy alone ----


_OPAQUE_CHAT = OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="3f0a9c1e-5f4d-4a1b")
_SYNTHETIC_CHAT = OwnerScope(owner_id="global", agent_id="main", surface_id="a2a", chat_id="-4001")


def _published(monkeypatch, scope, tmp_path):
    """Every event the three publishers emit for one conversation."""
    events: list[tuple] = []
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    bridge = message_bus.LocalChatBridge(RecordingOutbox(), drive_root=tmp_path)
    bridge.send_message("the answer", source="browser", scope=scope)
    bridge.send_chat_action(scope, source="browser")
    bridge.send_image(
        StoredFile(stored_file(scope=scope, filename="image.png", mime="image/png")["id"]),
        source="browser",
        scope=scope,
    )
    return events


def test_the_chat_topics_are_published_for_an_opaque_conversation(monkeypatch, tmp_path):
    """A granted ``subscribe_event:chat.outbound`` keeps delivering whatever a chat id spells.

    The old gate dropped the event when the id was not a number — no error and no log line,
    which is a revoked subscription a skill has no way to notice.
    """
    events = _published(monkeypatch, _OPAQUE_CHAT, tmp_path)

    assert [topic for topic, _ in events] == [event_bus.CHAT_OUTBOUND, event_bus.CHAT_TYPING, event_bus.CHAT_PHOTO]
    # The axis the row records, not a number invented for the payload.
    assert {data["chat_id"] for _, data in events} == {"3f0a9c1e-5f4d-4a1b"}


def test_the_chat_topics_stay_suppressed_for_a_synthetic_conversation(monkeypatch, tmp_path):
    """A bridge or A2A conversation is a routing fact and has no chat event to publish."""
    assert _published(monkeypatch, _SYNTHETIC_CHAT, tmp_path) == []
