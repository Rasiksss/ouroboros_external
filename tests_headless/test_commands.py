from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.chat_commands import ConfirmationAnswer
from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.commands import handle_confirmation_reply
from supervisor.orchestrator.intent import TaskIntentKind
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.sql_faults import fail_sql
from tests.helpers.task_stop_state import sql_task_stop_reader
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _scope(source: str, chat_id: int | str) -> OwnerScope:
    """The scope a transport would build for a message arriving on ``source``."""
    return OwnerScope(
        owner_id=configured_owner_id(),
        agent_id=configured_agent_id(),
        surface_id=SurfaceId(source),
        chat_id=str(chat_id),
    )


#: The scope the rows in this module are written under. Messages are keyed by the owner,
#: the agent and the chat, so a read minted for another conversation sees none of them.
TEST_SCOPE = _scope("browser", 1)

DIALOGS_SCOPE = _scope("dialogs", 1)


def test_runtime_command_parser_preserves_legacy_prefixes_and_new_command_boundaries() -> None:
    from ouroboros.contracts.chat_commands import runtime_command_parts, runtime_slash_command

    assert runtime_slash_command("/statusplease") == "/status"
    assert runtime_command_parts("/RestartNow") == ("/restart", "Now")
    assert runtime_slash_command("/newjob") == ""
    assert runtime_slash_command("/stopjob") == ""
    assert runtime_command_parts('  /new  "Keep Original Case"  ') == ("/new", '"Keep Original Case"  ')
    assert runtime_command_parts("/stop job-1") == ("/stop", "job-1")


def test_job_commands_render_russian_summary_and_idempotent_current_stop_state(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job-1", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    ctx, sent = _command_context(tmp_path, store)

    handle_slash_command("/jobs", ctx=ctx, scope=TEST_SCOPE, source="browser")
    handle_slash_command("/stop job-1", ctx=ctx, scope=TEST_SCOPE, source="browser")
    handle_slash_command("/stop job-1", ctx=ctx, scope=TEST_SCOPE, source="browser")

    assert "в очереди" in sent[0][0][0]
    assert "создана:" in sent[0][0][0] and "запущена:" in sent[0][0][0]
    assert "created_at=" not in sent[0][0][0] and "started_at=" not in sent[0][0][0]
    assert sent[1][0] == sent[2][0] == ("🛑 Задача job-1 остановлена.",)


def test_stop_reports_an_already_completed_job_explicitly(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="finished-job", detached=True, text="work", scope=TEST_SCOPE.to_dict()), status="completed"
    )
    ctx, sent = _command_context(tmp_path, store)

    handle_slash_command("/stop finished-job", ctx=ctx, scope=TEST_SCOPE, source="browser")

    assert sent[0][0] == ("Задача finished-job уже завершена: выполнена.",)


def test_job_command_errors_are_user_facing_russian(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    ctx, sent = _command_context(tmp_path, runtime_persistence.initialize())
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "false")

    handle_slash_command("/jobs", ctx=ctx, scope=TEST_SCOPE, source="browser")
    missing_task = handle_slash_command("/new", ctx=ctx, scope=TEST_SCOPE, source="browser")

    assert sent[0][0] == ("Активных задач нет.",)
    assert missing_task.message == "После команды /new укажите задачу."


@parametrize_from_dict(
    {
        "browser-accepted": {"refused": False, "entry": "browser"},
        "slash-accepted": {"refused": False, "entry": "slash"},
        "browser-refused": {"refused": True, "entry": "browser"},
        "slash-refused": {"refused": True, "entry": "slash"},
    }
)
def test_admission_replay_resumes_only_unaccepted_reply_in_original_trace(tmp_path, monkeypatch, refused, entry):
    import threading
    from dataclasses import replace

    from ouroboros.observability import aef_tracing
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.settings.schema import AppSettings
    from supervisor import message_bus
    from supervisor.runtime import SupervisorLoop
    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests, supervisor_heartbeat
    from transport.interfaces import InboundEnvelope

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    settings = AppSettings(
        OUROBOROS_DURABLE_JOBS_ENABLED=True,
        OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER=1,
        OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER=1,
    )
    monkeypatch.setattr("supervisor.orchestrator.orchestrator.load_settings", lambda: settings)
    if refused:
        runtime_persistence.current.tasks_repo.create_task(
            TaskRequest(id="busy", text="busy", scope={**TEST_SCOPE.to_dict(), "agent_id": "another-agent"})
        )
    ctx, _ = _command_context(tmp_path, runtime_persistence.current)
    ctx.state_repo, ctx.orchestrator = runtime.state_repo, runtime.orchestrator
    ctx.bridge = runtime.bridge
    ctx.drive_root, ctx.task_queue, ctx.agent_events = tmp_path, runtime.task_queue, runtime.agent_events
    ctx.send_with_budget = message_bus.send_with_budget
    loop = SupervisorLoop(
        ctx=ctx,
        stop_event=threading.Event(),
        inbox=runtime.gateway.inbox,
        heartbeat=supervisor_heartbeat(),
    )
    trace = "12345678-1234-5678-1234-567812345678"
    envelope = InboundEnvelope(
        scope=TEST_SCOPE,
        user_id=1,
        client_message_id="admission",
        text="/new work" if entry == "slash" else "work",
        detached=entry == "browser",
        trace_id=trace,
        transport={"source_message_id": "original"},
    )
    published = []
    monkeypatch.setattr(
        runtime.gateway.outbox, "publish", lambda _m, **kw: (_ for _ in ()).throw(OSError("outbox down"))
    )
    try:
        loop.inbox.submit(envelope)
        loop.process_inbox(timeout=0)
        history = sql_conversation_repository(runtime_persistence.current.engine).list_messages(
            scope=TEST_SCOPE, actor_type="agent"
        )
        assert len(history) == 1 and published == []
        monkeypatch.setattr(
            runtime.gateway.outbox, "publish", lambda m, **kw: published.append((m, aef_tracing.current_trace_id()))
        )
        monkeypatch.setattr("supervisor.runtime._INBOX_POLL_TIMEOUT_SEC", 0)
        loop.run_tick()
        assert len(published) == 1
        settings.OUROBOROS_DURABLE_JOBS_ENABLED = False
        if refused:
            runtime_persistence.current.tasks_repo.update_task_status("busy", "completed")
        loop.inbox.submit(replace(envelope, trace_id="87654321-1234-5678-1234-567812345678", transport={}))
        loop.process_inbox(timeout=0)
        loop._publish_terminal_jobs()
        assert len(published) == 1
        assert len(runtime_persistence.current.tasks_repo.list_tasks()) == (2 if refused else 1)
        message, active_trace = published[0]
        assert message.trace_id == active_trace == trace
        assert message.transport == envelope.transport
        expected = (
            message.text == "Не удалось запустить фоновую задачу: достигнут лимит активных задач."  # noqa: RUF001
            if refused
            else message.text == "Фоновая задача принята. Результат появится здесь после завершения."
        )
        assert expected and message.task_id
        if refused:
            assert runtime_persistence.current.tasks_repo.get_task(message.task_id).terminal_published_at
        assert [
            (m.content, m.created_at, m.message_id)
            for m in sql_conversation_repository(runtime_persistence.current.engine).list_messages(
                scope=TEST_SCOPE, actor_type="agent"
            )
        ] == [(m.content, m.created_at, m.message_id) for m in history]
    finally:
        reset_runtime_for_tests()


def _command_context(tmp_path, store, *, tasks_repo=None, tool_confirmation_reader=None):
    """A context with a store and nothing else: `/stop` addresses records, not objects."""
    sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        drive_root=tmp_path,
        conversations_repo=runtime_persistence.current.conversations_repo,
        result_repo=runtime_persistence.current.result_repo,
        tool_confirmation_reader=tool_confirmation_reader
        if tool_confirmation_reader is not None
        else sql_tool_confirmation_reader(),
        tasks_repo=tasks_repo if tasks_repo is not None else sql_task_stop_reader(),
        executor=SimpleNamespace(
            get_slot=lambda _worker_id: pytest.fail("a stop must not be decided from a worker slot")
        ),
        send_with_budget=lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    return ctx, sent


def test_stop_with_no_running_record_says_there_is_none(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    ctx, sent = _command_context(tmp_path, runtime_persistence.initialize())
    transport = {"target_uid": "owner-7", "reply_to": "message-9"}

    handled = handle_slash_command(
        " /stop ",
        ctx=ctx,
        scope=TEST_SCOPE,
        source="dialogs",
        transport=transport,
    )

    assert handled is True
    assert sent == [
        (
            ("Активной задачи нет.",),
            {"scope": TEST_SCOPE, "source": "dialogs", "transport": transport},
        )
    ]


def test_stop_records_the_intent_on_the_owner_s_own_run(tmp_path, monkeypatch) -> None:
    """The record carries the stop, so the run ends wherever it is held."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    store = runtime_persistence.initialize()
    claim_task(store, "mine")
    ctx, sent = _command_context(tmp_path, store)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("mine").stop_requested_at
    assert sent[0][0] == ("🛑 Остановка запрошена. Задача завершится на ближайшей безопасной точке.",)


def test_stop_publishes_an_addressed_hint_after_the_durable_write(tmp_path, monkeypatch) -> None:
    from new_core.coordination.interfaces import TaskStopNotice
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "false")
    store = runtime_persistence.initialize()
    claim_task(store, "mine")
    ctx, _sent = _command_context(tmp_path, store)
    notices: list[TaskStopNotice] = []
    ctx.task_stop_publisher = SimpleNamespace(publish=notices.append)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("mine").stop_requested_at
    assert notices == [TaskStopNotice(runtime_id="runtime-a", task_ids=("mine",))]


def test_named_stop_publishes_hints_for_running_descendants_and_counts_roots(tmp_path, monkeypatch) -> None:
    from new_core.coordination.interfaces import TaskStopNotice
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    store = runtime_persistence.initialize()
    claim_task(store, "root")
    store.tasks_repo.create_task(
        TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict())
    )
    child = store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-b")
    assert child is not None and child.task_id == "child"
    ctx, sent = _command_context(tmp_path, store)
    notices: list[TaskStopNotice] = []
    ctx.task_stop_publisher = SimpleNamespace(publish=notices.append)

    assert handle_slash_command("/stop root", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("root").stop_requested_at
    assert store.tasks_repo.get_task("child").stop_requested_at
    assert notices == [
        TaskStopNotice(runtime_id="runtime-a", task_ids=("root",)),
        TaskStopNotice(runtime_id="runtime-b", task_ids=("child",)),
    ]
    assert sent[0][0] == ("🛑 Остановка задачи root запрошена.",)


def test_named_stop_reports_a_descendant_stopped_below_a_terminal_root(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="completed")
    store.tasks_repo.create_task(
        TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict()),
        status="scheduled",
    )
    ctx, sent = _command_context(tmp_path, store)

    assert handle_slash_command("/stop root", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("child").status == "cancelled"
    assert sent[0][0] == ("🛑 Задача root остановлена.",)


def test_default_stop_closes_running_trees_and_the_chats_queue(tmp_path, monkeypatch) -> None:
    from new_core.coordination.interfaces import TaskStopNotice
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "false")
    store = runtime_persistence.initialize()
    claim_task(store, "root")
    store.tasks_repo.create_task(
        TaskRequest(id="running-child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict())
    )
    child = store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-b")
    assert child is not None and child.task_id == "running-child"
    store.tasks_repo.create_task(
        TaskRequest(id="waiting-child", parent_task_id="root", text="later", scope=TEST_SCOPE.to_dict())
    )
    store.tasks_repo.create_task(TaskRequest(id="unrelated", text="other", scope=TEST_SCOPE.to_dict()))
    ctx, sent = _command_context(tmp_path, store)
    notices: list[TaskStopNotice] = []
    ctx.task_stop_publisher = SimpleNamespace(publish=notices.append)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("root").stop_requested_at
    assert store.tasks_repo.get_task("running-child").stop_requested_at
    assert store.tasks_repo.get_task("waiting-child").status == "cancelled"
    assert store.tasks_repo.get_task("unrelated").status == "cancelled"
    assert notices == [
        TaskStopNotice(runtime_id="runtime-a", task_ids=("root",)),
        TaskStopNotice(runtime_id="runtime-b", task_ids=("running-child",)),
    ]
    assert sent[0][0] == ("🛑 Остановка запрошена. Задача завершится на ближайшей безопасной точке.",)


def test_a_stop_hint_failure_does_not_undo_the_durable_stop(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "false")
    store = runtime_persistence.initialize()
    claim_task(store, "mine")
    ctx, sent = _command_context(tmp_path, store)

    def fail(_notice) -> None:
        assert store.tasks_repo.get_task("mine").stop_requested_at
        raise OSError("broker unavailable")

    ctx.task_stop_publisher = SimpleNamespace(publish=fail)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("mine").stop_requested_at
    assert sent[0][0] == ("🛑 Остановка запрошена. Задача завершится на ближайшей безопасной точке.",)


def test_stop_does_not_reach_another_owner_s_run(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    store = runtime_persistence.initialize()
    other = OwnerScope(
        owner_id="someone-else",
        agent_id=TEST_SCOPE.agent_id,
        surface_id=TEST_SCOPE.surface_id,
        chat_id=TEST_SCOPE.chat_id,
    )
    claim_task(store, "theirs", scope=other)
    ctx, sent = _command_context(tmp_path, store)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("theirs").stop_requested_at is None
    assert sent == [
        (("Активной задачи нет.",), {"scope": TEST_SCOPE, "source": "dialogs"}),
    ]


def test_a_second_stop_says_the_run_is_already_stopping(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    store = runtime_persistence.initialize()
    claim_task(store, "mine")
    ctx, sent = _command_context(tmp_path, store)

    handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs")
    first = store.tasks_repo.get_task("mine").stop_requested_at
    handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert store.tasks_repo.get_task("mine").stop_requested_at == first
    assert "уже запрошена" in sent[1][0][0]


def test_a_run_that_finishes_before_the_tree_write_gets_no_stale_hint_or_stop_reply(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    store = runtime_persistence.initialize()
    running = claim_task(store, "mine")

    def finish_before_stop(*, scope, job_id=None):
        assert scope == TEST_SCOPE and job_id is None
        store.tasks_repo.complete_task("mine", result="won")
        return [store.tasks_repo.get_task("mine")]

    notices = []
    ctx, sent = _command_context(tmp_path, store)
    ctx.tasks_repo = SimpleNamespace(
        list_tasks=lambda **_kwargs: [running],
        get_task=store.tasks_repo.get_task,
        stop_jobs=finish_before_stop,
        request_task_stop=store.tasks_repo.request_task_stop,
    )
    ctx.task_stop_publisher = SimpleNamespace(publish=notices.append)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert notices == []
    assert sent[0][0] == ("Активной задачи нет.",)


def test_durable_stop_does_not_count_a_root_that_finished_before_the_tree_write(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    store = runtime_persistence.initialize()
    claim_task(store, "mine")

    def finish_before_stop(*, scope, job_id=None):
        assert scope == TEST_SCOPE and job_id is None
        store.tasks_repo.complete_task("mine", result="won")
        return []

    ctx, sent = _command_context(tmp_path, store)
    ctx.tasks_repo = SimpleNamespace(stop_jobs=finish_before_stop, list_tasks=store.tasks_repo.list_tasks)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert sent[0][0] == ("Активной задачи нет.",)


def test_durable_stop_without_a_job_stops_descendants_of_a_completed_root(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="completed")
    store.tasks_repo.create_task(
        TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict()),
        status="scheduled",
    )
    ctx, sent = _command_context(tmp_path, store)

    assert handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True

    assert store.tasks_repo.get_task("child").status == "cancelled"
    assert sent[0][0] == ("🛑 Остановка запрошена. Задача завершится на ближайшей безопасной точке.",)


def test_an_owner_restart_closes_only_the_askers_own_runs(tmp_path) -> None:
    """`/restart` means "do not resume mine". It never speaks for anybody else's task."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import _cancel_own_runs

    store = runtime_persistence.initialize()
    claim_task(store, "mine", scope=TEST_SCOPE)
    other = OwnerScope(
        owner_id="another-owner",
        agent_id=configured_agent_id(),
        surface_id=SurfaceId("browser"),
        chat_id="99",
    )
    claim_task(store, "theirs", scope=other)
    ctx, _sent = _command_context(tmp_path, store)

    _cancel_own_runs(ctx, scope=TEST_SCOPE)

    assert store.tasks_repo.get_task("mine").status == "cancelled"
    assert store.tasks_repo.get_task("theirs").status == "running"


def test_headless_restart_does_not_drain_between_owner_snapshot_and_close(tmp_path, monkeypatch) -> None:
    """The process drain starts only after `/restart` has made the owner's runs no-resume."""
    from threading import Event, Thread
    from types import SimpleNamespace

    import server
    from new_core.coordination.inprocess import NullWorkSignal
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=600,
        hard_timeout=1800,
        audit_event_repository=sql_audit_event_repository(),
    )
    claim_task(store, "running")

    restart_started = Event()
    owner_snapshot = Event()
    release_finished = Event()
    failures: list[BaseException] = []
    release_thread: Thread | None = None

    def request_restart() -> None:
        nonlocal release_thread
        if release_thread is not None:
            return

        def release() -> None:
            try:
                assert owner_snapshot.wait(2)
                queue.release_own_runs(runtime_id="runtime-a")
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                release_finished.set()

        release_thread = Thread(target=release)
        release_thread.start()
        restart_started.set()

    monkeypatch.setattr(server, "_request_restart_exit", request_restart)

    def list_tasks(**kwargs):
        records = store.tasks_repo.list_tasks(**kwargs)
        owner_snapshot.set()
        if restart_started.is_set():
            assert release_finished.wait(2)
        return records

    ctx = SimpleNamespace(
        tasks_repo=SimpleNamespace(
            list_tasks=list_tasks, cancel_task=store.tasks_repo.cancel_task, stop_jobs=store.tasks_repo.stop_jobs
        ),
        send_with_budget=lambda *_args, **_kwargs: None,
        safe_restart=server.safe_restart,
        request_restart=request_restart,
    )

    handle_slash_command("/restart", ctx=ctx, scope=TEST_SCOPE, source="browser")

    assert release_thread is not None
    release_thread.join(2)
    assert not release_thread.is_alive()
    assert failures == []
    assert [record for record in store.tasks_repo.list_tasks(statuses=["queued"]) if record.task_id != "running"] == []


def test_restart_closes_a_successor_released_after_its_owner_snapshot(tmp_path) -> None:
    """An independent drain cannot reintroduce a job after `/restart` has listed it."""
    from types import SimpleNamespace

    from new_core.coordination.inprocess import NullWorkSignal
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=600,
        hard_timeout=1800,
        audit_event_repository=sql_audit_event_repository(),
    )
    claim_task(store, "running")

    def list_tasks(**kwargs):
        records = store.tasks_repo.list_tasks(**kwargs)
        queue.release_own_runs(runtime_id="runtime-a")
        return records

    ctx = SimpleNamespace(
        tasks_repo=SimpleNamespace(
            list_tasks=list_tasks, cancel_task=store.tasks_repo.cancel_task, stop_jobs=store.tasks_repo.stop_jobs
        ),
        send_with_budget=lambda *_args, **_kwargs: None,
        safe_restart=lambda **_kwargs: (True, "restart requested"),
        request_restart=lambda: None,
    )

    handle_slash_command("/restart", ctx=ctx, scope=TEST_SCOPE, source="browser")

    assert [record for record in store.tasks_repo.list_tasks(statuses=["queued"]) if record.task_id != "running"] == []


def test_an_owner_restart_also_closes_the_askers_queued_work(tmp_path) -> None:
    """A restart that does not resume: a queued record left behind is claimed straight back.

    The instance drain does not cover it — it hands back what it holds and leaves the queue
    alone — so this cancellation is the whole mechanism.
    """
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import _cancel_own_runs

    store = runtime_persistence.initialize()
    claim_task(store, "mine", scope=TEST_SCOPE)
    store.tasks_repo.create_task(
        TaskRequest(id="queued-mine", type="task", text="later", chat_id=1, scope=TEST_SCOPE.to_dict())
    )
    other = OwnerScope(
        owner_id="another-owner",
        agent_id=configured_agent_id(),
        surface_id=SurfaceId("browser"),
        chat_id="99",
    )
    store.tasks_repo.create_task(
        TaskRequest(id="queued-theirs", type="task", text="later", chat_id=99, scope=other.to_dict())
    )
    ctx, _sent = _command_context(tmp_path, store)

    _cancel_own_runs(ctx, scope=TEST_SCOPE)

    assert store.tasks_repo.get_task("mine").status == "cancelled"
    assert store.tasks_repo.get_task("queued-mine").status == "cancelled"
    assert store.tasks_repo.get_task("queued-theirs").status == "queued"


def test_an_owner_restart_closes_more_than_the_default_listing_batch(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import _cancel_own_runs

    store = runtime_persistence.initialize()
    for index in range(501):
        store.tasks_repo.create_task(
            TaskRequest(
                id=f"child-{index:03d}",
                type="subtask",
                text="later",
                parent_task_id="parent",
                scope=TEST_SCOPE.to_dict(),
            )
        )
    ctx, _sent = _command_context(tmp_path, store)

    _cancel_own_runs(ctx, scope=TEST_SCOPE)

    assert store.tasks_repo.list_tasks(statuses=["queued"], scope=TEST_SCOPE, limit=None) == []
    assert store.tasks_repo.get_task("child-500").status == "cancelled"


def test_a_command_reply_with_no_deliverable_source_is_logged(tmp_path, caplog) -> None:
    """`unknown` is claimed by no transport, so the reply vanishes; it must not vanish quietly."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    ctx, sent = _command_context(tmp_path, runtime_persistence.initialize())

    with caplog.at_level("WARNING", logger="supervisor.commands"):
        handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="")

    assert sent[0][0][0] == "Активной задачи нет."
    assert "no deliverable source" in caplog.text


@pytest.mark.parametrize(
    "text",
    ["Почисти историю", "/panic"],
    ids=["plain-speech", "a-command-that-never-existed"],
)
def test_a_message_naming_no_command_of_the_runtime_is_ordinary_work(tmp_path, text: str) -> None:
    """Only a command the runtime owns or retired is the runtime's to answer."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests
    from transport.interfaces import InboundEnvelope

    orchestrator = bootstrap_supervisor_for_tests(tmp_path).orchestrator
    envelope = InboundEnvelope(scope=_scope("browser", 1), user_id=1, text=text)
    ctx, sent = _command_context(tmp_path, runtime_persistence.initialize())

    assert orchestrator.classify(envelope) == (TaskIntentKind.AGENT_CHAT, None)
    assert handle_slash_command(text, ctx=ctx, scope=TEST_SCOPE, source="dialogs") is False
    assert sent == []


def _open_confirmation(store, *, task_id: str = "waiting", scope=TEST_SCOPE, confirmation_id: str = ""):
    claim_task(store, task_id, scope=scope)
    return park_run(store, task_id=task_id, scope=scope, confirmation_id=confirmation_id or f"c-{task_id}")


def _said(
    tmp_path, actor_type: str, text: str, *, at: int, client_message_id: str = "", transport=None, scope=TEST_SCOPE
) -> None:
    from ouroboros.storage.chat_store import append_chat_message

    append_chat_message(
        tmp_path,
        scope=scope,
        actor_type=actor_type,
        chat_id=scope.chat_id,
        user_id=1,
        text=text,
        source=str(scope.surface_id),
        client_message_id=client_message_id,
        transport=transport,
        ts=f"2026-01-01T00:00:0{at}Z",
    )


def _asked(tmp_path, confirmation_id: str, *, at: int = 1, scope=TEST_SCOPE) -> None:
    from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY

    _said(
        tmp_path,
        "agent",
        f"Подтвердите действие: data_write [{confirmation_id[:8]}]",
        at=at,
        transport={CONFIRMATION_TRANSPORT_KEY: {"confirmation_id": confirmation_id}},
        scope=scope,
    )


@pytest.mark.parametrize("decision", ["approved", "denied"])
def test_an_answer_is_recorded_and_queues_the_run_it_answers(tmp_path, caplog, decision) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_confirmation_reply

    store = runtime_persistence.initialize()
    _open_confirmation(store)
    ctx, sent = _command_context(tmp_path, store)

    with caplog.at_level(logging.INFO, logger="supervisor.commands"):
        handle_confirmation_reply(
            ConfirmationAnswer(decision, "c-waiting"), ctx=ctx, scope=TEST_SCOPE, source="dialogs"
        )

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("c-waiting") == decision
    assert store.tasks_repo.get_task("waiting").status == "queued"
    assert len(sent) == 1
    assert (
        f"event=tool_confirmation_resolved task_id=waiting tool=data_write confirmation_id=c-waiting decision={decision}"
        in caplog.messages
    )


def test_a_stop_reaches_a_run_that_is_waiting_for_an_answer(tmp_path) -> None:
    """The owner may stop instead of answering; the record is queued so a run can read the stop."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    store = runtime_persistence.initialize()
    _open_confirmation(store)
    ctx, sent = _command_context(tmp_path, store)

    handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    record = store.tasks_repo.get_task("waiting")
    assert (record.status, bool(record.stop_requested_at)) == ("queued", True)
    assert "остановк" in sent[0][0][0].lower()


def test_the_first_answer_decides_and_is_the_only_outcome_recorded(tmp_path) -> None:
    """The decision is taken once: the run may already be acting on the first answer."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_confirmation_reply

    store = runtime_persistence.initialize()
    _open_confirmation(store)
    ctx, sent = _command_context(tmp_path, store)

    handle_confirmation_reply(ConfirmationAnswer("approved", "c-waiting"), ctx=ctx, scope=TEST_SCOPE, source="dialogs")
    handle_confirmation_reply(ConfirmationAnswer("denied", "c-waiting"), ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("c-waiting") == "approved"
    resolved = sql_audit_event_repository(store.engine).list_audit_events(
        event_type="policy.tool_confirmation_resolved", limit=10
    )
    assert [(e.metadata["confirmation_id"], e.metadata["decision"]) for e in resolved] == [("c-waiting", "approved")]
    assert sent[1][0][0] == "Нечего подтверждать: активного запроса нет."


def test_an_answer_naming_this_request_decides_it(tmp_path) -> None:
    """A client answers by id — the head of it, which is what the question showed the owner."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_confirmation_reply

    store = runtime_persistence.initialize()
    _open_confirmation(store, confirmation_id="ab" * 16)
    ctx, _sent = _command_context(tmp_path, store)

    handle_confirmation_reply(ConfirmationAnswer("approved", "abababab"), ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("ab" * 16) == "approved"


@parametrize_from_dict(
    {
        "named": {"answered": "cdcdcdcd"},
        "bare-read-as-answering-the-previous-request": {"answered": "cd" * 16},
        "read-as-answering-nothing": {"answered": ""},
    }
)
def test_an_answer_to_another_request_decides_nothing(tmp_path, answered) -> None:
    """Deciding whatever is open now would allow a call this answer was never given for."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_confirmation_reply

    store = runtime_persistence.initialize()
    _open_confirmation(store, confirmation_id="ab" * 16)
    ctx, sent = _command_context(tmp_path, store)

    handle_confirmation_reply(ConfirmationAnswer("approved", answered), ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("ab" * 16) == "pending"
    assert sent[0][0][0] == "Этот запрос уже не ждёт ответа."


def test_an_answer_the_store_could_not_take_is_reported_to_the_owner(tmp_path) -> None:
    store = runtime_persistence.current
    _open_confirmation(store)
    ctx, sent = _command_context(tmp_path, store)

    with fail_sql("UPDATE tool_confirmations SET status", OperationalError("UPDATE", {}, Exception("no connection"))):
        handle_confirmation_reply(
            ConfirmationAnswer("approved", "c-waiting"), ctx=ctx, scope=TEST_SCOPE, source="dialogs"
        )

    assert "не записан" in sent[0][0][0]
    assert sql_tool_confirmation_reader().read_tool_confirmation_status("c-waiting") == "pending"
    assert store.tasks_repo.get_task("waiting").status == "waiting_confirmation"


@pytest.mark.parametrize(
    ("settled", "told"),
    [("approved", "Этот запрос уже решён."), ("expired", "истекло")],
    ids=["another-answer", "closed-unanswered"],
)
def test_an_answer_that_lost_the_write_leaves_the_run_parked_and_says_what_settled_it(tmp_path, settled, told) -> None:
    """A requeue would rerun the round with no decision; naming one for an expired request reports agreement nobody gave."""
    from supervisor.commands import handle_confirmation_reply

    store = runtime_persistence.current
    _open_confirmation(store)
    repository = sql_tool_confirmation_reader()
    reader = Mock(wraps=repository)

    def competing_answer(confirmation_id, *, decision, now_iso):
        competitor = sql_tool_confirmation_reader()
        if settled == "expired":
            assert competitor.expire_tool_confirmation(confirmation_id, now_iso=now_iso)
        else:
            assert competitor.resolve_tool_confirmation(confirmation_id, decision=settled, now_iso=now_iso)
        return repository.resolve_tool_confirmation(confirmation_id, decision=decision, now_iso=now_iso)

    reader.resolve_tool_confirmation.side_effect = competing_answer
    ctx, sent = _command_context(tmp_path, store, tool_confirmation_reader=reader)

    handle_confirmation_reply(ConfirmationAnswer("approved", "c-waiting"), ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert told in sent[0][0][0]
    reader.requeue_parked_task.assert_not_called()
    assert store.tasks_repo.get_task("waiting").status == "waiting_confirmation"
    assert repository.read_tool_confirmation_status("c-waiting") == settled


def test_an_answer_from_another_scope_is_not_a_decision(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_confirmation_reply

    store = runtime_persistence.initialize()
    _open_confirmation(store)
    ctx, _sent = _command_context(tmp_path, store)
    stranger = OwnerScope(
        owner_id="someone-else",
        agent_id=TEST_SCOPE.agent_id,
        surface_id=TEST_SCOPE.surface_id,
        chat_id=TEST_SCOPE.chat_id,
    )

    handle_confirmation_reply(ConfirmationAnswer("approved", "c-waiting"), ctx=ctx, scope=stranger, source="dialogs")

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("c-waiting") == "pending"


@parametrize_from_dict(
    {
        "bare-nothing-open": {"text": "да", "opened": False, "attached": False, "answered": ""},
        "bare-open": {"text": "да", "opened": True, "attached": False, "answered": "ab" * 16},
        "named-nothing-open": {
            "text": "/confirm abababab yes",
            "opened": False,
            "attached": False,
            "answered": "abababab",
        },
        "bare-photo": {"text": "да", "opened": True, "attached": True, "answered": ""},
        "digits-photo": {"text": "ок 2024", "opened": True, "attached": True, "answered": ""},
        "named-photo": {"text": "/confirm abababab yes", "opened": True, "attached": True, "answered": ""},
        "prefix": {"text": "да, и заодно удали остальные файлы", "opened": True, "attached": False, "answered": ""},
    }
)
def test_what_the_orchestrator_reads_as_an_answer(tmp_path, text, opened, attached, answered) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from transport.interfaces import InboundEnvelope

    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    orchestrator.conversations_repo = runtime_persistence.current.conversations_repo
    orchestrator.task_queue = SimpleNamespace(drive_root=tmp_path)
    if opened:
        persistence = runtime_persistence.current
        orchestrator.tool_confirmation_reader = sql_tool_confirmation_reader(persistence.engine)
        _open_confirmation(persistence, confirmation_id="ab" * 16, scope=DIALOGS_SCOPE)
        _asked(tmp_path, "ab" * 16, scope=DIALOGS_SCOPE)
        _said(tmp_path, "owner", text, at=2, client_message_id="reply-1", scope=DIALOGS_SCOPE)
    else:
        orchestrator.tool_confirmation_reader = sql_tool_confirmation_reader()
    envelope = InboundEnvelope(
        scope=DIALOGS_SCOPE, user_id=1, text=text, client_message_id="reply-1", image_base64="aGk=" if attached else ""
    )

    expected = (
        (TaskIntentKind.CONFIRMATION_REPLY, ConfirmationAnswer("approved", answered))
        if answered
        else (TaskIntentKind.AGENT_CHAT, None)
    )
    assert orchestrator.classify(envelope) == expected


@parametrize_from_dict(
    {
        "bare": {"text": "да", "intent": TaskIntentKind.AGENT_CHAT},
        "named": {"text": "да abababab", "intent": TaskIntentKind.AGENT_CHAT},
        "confirm-command": {"text": "/confirm abababab yes", "intent": TaskIntentKind.CONFIRMATION_REPLY},
    }
)
def test_a_typed_answer_in_the_browser_decides_nothing(tmp_path, text, intent) -> None:
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from transport.interfaces import InboundEnvelope

    persistence = runtime_persistence.current
    _open_confirmation(persistence, confirmation_id="ab" * 16)
    _asked(tmp_path, "ab" * 16)
    _said(tmp_path, "owner", text, at=2, client_message_id="reply-1")
    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    orchestrator.conversations_repo = persistence.conversations_repo
    orchestrator.tool_confirmation_reader = sql_tool_confirmation_reader(persistence.engine)
    orchestrator.task_queue = SimpleNamespace(drive_root=tmp_path)
    envelope = InboundEnvelope(scope=TEST_SCOPE, user_id=1, text=text, client_message_id="reply-1")

    assert orchestrator.classify(envelope) == (intent, None)


@parametrize_from_dict(
    {
        "dialogs": {"scope": DIALOGS_SCOPE, "told": "Ответ не распознан: нужно /confirm <id> да или нет."},
        "browser": {"scope": TEST_SCOPE, "told": "Подтвердите или отклоните действие кнопкой на карточке запроса."},
        "a2a": {"scope": _scope("a2a", 1), "told": "Нечего подтверждать: активного запроса нет."},
    }
)
def test_a_confirm_command_that_decides_nothing_is_answered_per_surface(tmp_path, scope, told) -> None:
    from supervisor.commands import handle_confirmation_reply
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from transport.interfaces import InboundEnvelope

    text = "/confirm 1234 yes"
    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    orchestrator.conversations_repo = runtime_persistence.current.conversations_repo
    orchestrator.tool_confirmation_reader = sql_tool_confirmation_reader()
    orchestrator.task_queue = SimpleNamespace(drive_root=tmp_path)
    ctx, sent = _command_context(tmp_path, runtime_persistence.current)

    intent, answer = orchestrator.classify(
        InboundEnvelope(scope=scope, user_id=1, text=text, client_message_id="reply-1")
    )
    handle_confirmation_reply(answer, ctx=ctx, scope=scope, source=str(scope.surface_id))

    assert intent == TaskIntentKind.CONFIRMATION_REPLY
    assert sent[0][0][0] == told


@parametrize_from_dict(
    {
        "right-after": {"history": ["question", "reply"], "reply": True},
        "reply-not-stored": {"history": ["question"], "reply": False},
        "owner-spoke-since": {"history": ["question", ("owner", "что внутри файла?"), "reply"], "reply": False},
        "agent-spoke-since": {"history": ["question", ("agent", "Показать подробнее?"), "reply"], "reply": False},
        "reply-sent-before-the-question": {"history": ["reply", "question"], "reply": False},
        "owner-spoke-after-the-reply": {"history": ["question", "reply", ("owner", "и ещё вопрос")], "reply": True},
    }
)
def test_a_bare_answer_counts_only_right_after_the_question(tmp_path, history, reply) -> None:
    """Said later, a bare word answers whatever was asked last; only the id names the request."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from transport.interfaces import InboundEnvelope

    store = runtime_persistence.initialize()
    _open_confirmation(store, confirmation_id="ab" * 16, scope=DIALOGS_SCOPE)
    for at, row in enumerate(history, start=1):
        if row == "question":
            _asked(tmp_path, "ab" * 16, at=at, scope=DIALOGS_SCOPE)
        elif row == "reply":
            _said(tmp_path, "owner", "да", at=at, client_message_id="reply-1", scope=DIALOGS_SCOPE)
        else:
            _said(tmp_path, *row, at=at, scope=DIALOGS_SCOPE)
    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    orchestrator.conversations_repo = runtime_persistence.current.conversations_repo
    orchestrator.tool_confirmation_reader = sql_tool_confirmation_reader(store.engine)
    orchestrator.task_queue = SimpleNamespace(drive_root=tmp_path)
    envelope = InboundEnvelope(scope=DIALOGS_SCOPE, user_id=1, text="да", client_message_id="reply-1")

    expected = (
        (TaskIntentKind.CONFIRMATION_REPLY, ConfirmationAnswer("approved", "ab" * 16))
        if reply
        else (TaskIntentKind.AGENT_CHAT, None)
    )
    assert orchestrator.classify(envelope) == expected


@pytest.mark.parametrize("text", ["/clean", "/CLEAN now"], ids=["bare", "cased-with-argument"])
def test_a_retired_command_is_refused_rather_than_worked_on(tmp_path, text: str) -> None:
    """It must not reach the agent, which answers as if the command had run."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command
    from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests
    from transport.interfaces import InboundEnvelope

    orchestrator = bootstrap_supervisor_for_tests(tmp_path).orchestrator
    envelope = InboundEnvelope(scope=_scope("browser", 1), user_id=1, text=text)
    ctx, sent = _command_context(tmp_path, runtime_persistence.initialize())

    result = orchestrator.submit(envelope)

    assert result.intent == TaskIntentKind.SLASH_COMMAND
    assert result.mode == SubmissionMode.NONE
    assert result.slash_command == "/clean"
    assert orchestrator.tasks_repo.list_tasks(scope=_scope("browser", 1), limit=None) == []
    assert handle_slash_command(result.slash_command, ctx=ctx, scope=TEST_SCOPE, source="dialogs") is True
    assert sent[0][0] == ("Команда /clean не поддерживается.",)


def _stored_overrides(scope) -> dict[str, str]:
    from sqlalchemy import text

    from ouroboros.storage.runtime_engine import resolve_runtime_engine

    statement = "SELECT key, value FROM user_settings WHERE owner_id = :owner_id AND agent_id = :agent_id"
    with resolve_runtime_engine("user settings").connect() as conn:
        rows = conn.execute(text(statement), {"owner_id": scope.owner_id, "agent_id": scope.agent_id}).mappings().all()
    return {str(row["key"]): str(row["value"]) for row in rows}


def test_hiding_the_reasoning_stores_the_asker_s_own_override(tmp_path) -> None:
    from config_registry.user_settings.reader import REASONING_PROGRESS_KEY
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    ctx, sent = _command_context(tmp_path, runtime_persistence.initialize())

    handled = handle_slash_command("/reasoning off", ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert handled is True
    assert _stored_overrides(TEST_SCOPE) == {REASONING_PROGRESS_KEY: "false"}
    assert len(sent) == 1
    assert sent[0][1] == {"scope": TEST_SCOPE, "source": "dialogs"}


def test_showing_the_reasoning_again_replaces_the_stored_value(tmp_path) -> None:
    from config_registry.user_settings.reader import REASONING_PROGRESS_KEY
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    ctx, _sent = _command_context(tmp_path, runtime_persistence.initialize())

    handle_slash_command("/reasoning off", ctx=ctx, scope=TEST_SCOPE, source="dialogs")
    handle_slash_command("/reasoning on", ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert _stored_overrides(TEST_SCOPE) == {REASONING_PROGRESS_KEY: "true"}


def test_the_bare_reasoning_command_stores_nothing_and_says_how_to_use_it(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_slash_command

    ctx, sent = _command_context(tmp_path, runtime_persistence.initialize())

    handled = handle_slash_command("/reasoning", ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert handled is True
    assert _stored_overrides(TEST_SCOPE) == {}
    assert "/reasoning on" in sent[0][0][0]


def test_the_reasoning_command_reaches_the_handler_with_its_argument(tmp_path) -> None:
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests
    from transport.interfaces import InboundEnvelope

    orchestrator = bootstrap_supervisor_for_tests(tmp_path).orchestrator
    envelope = InboundEnvelope(scope=_scope("browser", 1), user_id=1, text="/reasoning off")

    assert orchestrator.classify(envelope) == (TaskIntentKind.SLASH_COMMAND, None)
    assert TaskOrchestrator.submit(orchestrator, envelope).slash_command == "/reasoning off"


def test_asking_for_the_narration_in_words_is_work_rather_than_a_command() -> None:
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from transport.interfaces import InboundEnvelope

    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    orchestrator.conversations_repo = runtime_persistence.current.conversations_repo
    envelope = InboundEnvelope(scope=_scope("browser", 1), user_id=1, text="Скрой размышления")

    assert orchestrator.classify(envelope) == (TaskIntentKind.AGENT_CHAT, None)


@pytest.mark.parametrize("ending", ["cancel", "stop"])
def test_an_answer_to_a_run_that_will_not_act_on_it_records_no_agreement(tmp_path, ending) -> None:
    """A run that is over or stopping executes no tool body, so "allowed" would promise one."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.commands import handle_confirmation_reply

    store = runtime_persistence.initialize()
    _open_confirmation(store)
    if ending == "cancel":
        store.tasks_repo.cancel_task("waiting")
    else:
        store.tasks_repo.request_task_stop("waiting", scope=TEST_SCOPE)
    ctx, sent = _command_context(tmp_path, store)

    handle_confirmation_reply(ConfirmationAnswer("approved", "c-waiting"), ctx=ctx, scope=TEST_SCOPE, source="dialogs")

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("c-waiting") == "pending", (
        "the sweep closes it, and records that"
    )
    assert "не выполнено" in sent[0][0][0]
