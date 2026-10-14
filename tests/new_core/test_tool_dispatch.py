"""`dispatch.execute_tool`: the gates run once, before any provider, and hold nothing."""

from __future__ import annotations

import datetime
import pathlib
from contextlib import contextmanager
from typing import Any

import pytest

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.storage import task_transitions
from ouroboros.storage.runtime_models import ConversationMessage, TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.shared import TEST_SCOPE, make_catalog, make_tool_ctx, run_tool, static_tool_provider


class RecordingProvider(IToolProvider):
    """A provider that records every execute it is asked to perform."""

    default_runtime = THREAD_RUNTIME

    def __init__(self, name: str, origin: str, *, skips_llm_safety: bool = False, safety_policy: str = "check") -> None:
        self._desc = ToolDescriptor(
            schema=ToolSchema(name=name, description="d", parameters={}),
            timeout_sec=1,
            origin=origin,
            surface="discoverable",
            capability_class="read_only",
            safety_policy=safety_policy,
            skips_llm_safety=skips_llm_safety,
        )
        self.calls: list[tuple[Any, str, dict]] = []

    def refresh(self) -> None:
        return

    def descriptors(self, scope):
        return [self._desc]

    def get(self, name: str, scope):
        return self._desc if name == self._desc.name else None

    def refuse_dispatch(self, name: str, scope) -> str | None:
        return None

    def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
        self.calls.append((ctx, name, dict(args)))
        return f"ran {name}"


@pytest.fixture(autouse=True)
def _allow_safety(monkeypatch):
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", permissive_safety)


def _ctx(tmp_path: pathlib.Path, **kwargs) -> ToolContext:
    return make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, **kwargs)


def test_executor_preserves_parent_identity_and_merges_compaction_and_workstation_state(tmp_path):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.entry import ToolEntry
    from ouroboros.tools.compact_context import get_tools
    from tests.helpers.shared import run_tool_calls, static_tool_provider

    replacement, seen = object(), []

    def replace_workstation(ctx):
        seen.append(ctx.tool_call_id)
        ctx.workstation = replacement
        return "replaced"

    entry = ToolEntry(
        name="replace_workstation",
        schema=ToolSchema(name="replace_workstation", description="replace", parameters={}),
        handler=replace_workstation,
        safety_policy="skip",
    )
    ctx = _ctx(tmp_path, tool_call_id="parent-call", workstation=object())
    calls = [
        ToolCall(id="compact-call", function=ToolCallFunction(name="compact_context", arguments='{"keep_last_n":9}')),
        ToolCall(id="workstation-call", function=ToolCallFunction(name=entry.name, arguments="{}")),
    ]
    run_tool_calls(
        calls,
        make_catalog([static_tool_provider(*get_tools(), entry)]),
        tmp_path / "logs",
        "task",
        [],
        {"tool_calls": []},
        ctx=ctx,
    )
    assert ctx._pending_compaction == 9
    assert ctx.workstation is replacement
    assert seen == ["workstation-call"]
    assert ctx.tool_call_id == "parent-call"


@pytest.mark.parametrize("detached", [True, "true", False])
def test_schedule_task_rejects_removed_detached_argument(tmp_path, detached):
    from ouroboros.tools.service.orchestration import get_tools

    entry = next(tool for tool in get_tools() if tool.name == "schedule_task")
    assert "detached" not in entry.schema.parameters["properties"]
    ctx = _ctx(tmp_path, task_id="parent")
    catalog = make_catalog([static_tool_provider(entry)])
    result = run_tool(catalog, ctx, "schedule_task", {"description": "work", "detached": detached})
    assert "detached" in result and ("ERROR" in result or "Error" in result)
    assert ctx.pending_events == []


# --- 4.4 heal mode ----------------------------------------------------------------


# --- 4.5 no retained context ------------------------------------------------------


def test_execute_tool_holds_no_context_between_calls(tmp_path) -> None:
    provider = RecordingProvider("demo", "builtin")
    ctx_a = _ctx(tmp_path, task_id="a")
    ctx_b = _ctx(tmp_path, task_id="b")

    run_tool(make_catalog([provider]), ctx_a, "demo", {"n": 1})
    run_tool(make_catalog([provider]), ctx_b, "demo", {"n": 2})

    assert [call[0] for call in provider.calls] == [ctx_a, ctx_b]
    # Nothing from the first call leaked into the second.
    assert [call[2] for call in provider.calls] == [{"n": 1}, {"n": 2}]


def test_provider_does_not_retain_the_context(tmp_path) -> None:
    provider = RecordingProvider("demo", "builtin")
    run_tool(make_catalog([provider]), _ctx(tmp_path, task_id="a"), "demo", {})

    leaked = [attr for attr, value in vars(provider).items() if isinstance(value, ToolContext)]
    assert not leaked, f"provider retained context on {leaked}"


def test_a_closed_persisted_attempt_cannot_start_another_tool_effect(tmp_path) -> None:
    from ouroboros.agent.loop_tool_execution import ToolCallExecutor
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.shared import TEST_SCOPE

    store = runtime_persistence.current
    store.tasks_repo.create_task(TaskRequest(id="closed", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.fail_task("closed", result="done")
    provider = RecordingProvider("demo", "builtin", skips_llm_safety=True)

    result = run_tool(
        make_catalog([provider]),
        _ctx(tmp_path, task_id="closed", detached=True, current_task_type="task"),
        "demo",
        {},
    )

    assert result == "⚠️ TOOL_TASK_ATTEMPT_INACTIVE"
    assert ToolCallExecutor._is_tool_execution_failure(True, result)
    assert ToolCallExecutor._extract_result_metadata("demo", result, True)["error_code"] == "TOOL_TASK_ATTEMPT_INACTIVE"
    assert provider.calls == []


def test_a_missing_task_attempt_fails_closed(tmp_path) -> None:
    provider = RecordingProvider("demo", "builtin", skips_llm_safety=True)

    result = run_tool(
        make_catalog([provider]),
        _ctx(tmp_path, task_id="missing", detached=True, current_task_type="task"),
        "demo",
        {},
    )

    assert result == "⚠️ TOOL_TASK_ATTEMPT_INACTIVE"
    assert provider.calls == []


def test_an_unconfigured_runtime_store_error_is_not_an_inactive_attempt(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence

    runtime_persistence.close()
    provider = RecordingProvider("demo", "builtin", skips_llm_safety=True)

    with pytest.raises(RuntimeError):
        run_tool(
            make_catalog([provider]),
            _ctx(tmp_path, task_id="task", detached=True, current_task_type="task"),
            "demo",
            {},
        )
    assert provider.calls == []


def test_a_runtime_store_query_error_is_not_treated_as_an_inactive_attempt(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.current
    monkeypatch.setattr(
        store.tasks_repo,
        "is_task_attempt_active",
        lambda _task_id, **_kw: (_ for _ in ()).throw(RuntimeError("query failed")),
    )
    provider = RecordingProvider("demo", "builtin", skips_llm_safety=True)

    with pytest.raises(RuntimeError, match="query failed"):
        run_tool(
            make_catalog([provider]),
            _ctx(tmp_path, task_id="task", detached=True, current_task_type="task"),
            "demo",
            {},
        )

    assert provider.calls == []


def test_foreground_stop_during_safety_does_not_start_the_handler(tmp_path, monkeypatch) -> None:
    import ouroboros.policy.safety as safety

    stopped = False

    def stop_during_safety(*_args, **_kwargs):
        nonlocal stopped
        stopped = True
        return True, ""

    monkeypatch.setattr(safety, "check_safety", stop_during_safety)
    provider = RecordingProvider("demo", "builtin")
    result = run_tool(
        make_catalog([provider]),
        _ctx(tmp_path, task_id="task", current_task_type="task", stop_requested=lambda: stopped),
        "demo",
        {},
    )

    assert result == "⚠️ TOOL_TASK_ATTEMPT_INACTIVE"
    assert provider.calls == []


def test_foreground_tool_execution_does_not_read_the_attempt_store(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence

    def active_attempt(_task_id: str) -> bool:
        raise AssertionError("foreground execution must not query the attempt store")

    monkeypatch.setattr(runtime_persistence.current.tasks_repo, "is_task_attempt_active", active_attempt)
    provider = RecordingProvider("demo", "builtin", skips_llm_safety=True)

    result = run_tool(
        make_catalog([provider]),
        _ctx(tmp_path, task_id="task", current_task_type="task"),
        "demo",
        {},
    )

    assert result == "ran demo"


@pytest.mark.parametrize("drain_during_read", [False, True])
def test_detached_tool_rechecks_local_drain_after_authority_read(tmp_path, monkeypatch, drain_during_read) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import execution_claim

    stopped = []

    def active(task_id, **kwargs):
        stopped.append(drain_during_read)
        return True

    monkeypatch.setattr(runtime_persistence.current.tasks_repo, "is_task_attempt_active", active)
    provider = RecordingProvider("demo", "builtin", skips_llm_safety=True)
    token = execution_claim.set(("task", "claim", lambda: any(stopped)))
    try:
        result = run_tool(
            make_catalog([provider]),
            _ctx(tmp_path, task_id="task", detached=True, current_task_type="task", execution_claim="claim"),
            "demo",
            {},
        )
    finally:
        execution_claim.reset(token)
    assert result == ("⚠️ TOOL_TASK_ATTEMPT_INACTIVE" if drain_during_read else "ran demo")
    assert bool(provider.calls) is not drain_during_read


@pytest.mark.parametrize("loss", ["completed", "expired", "stop", "replaced"])
def test_task_attempt_is_rechecked_after_the_safety_gate(tmp_path, monkeypatch, loss) -> None:
    import ouroboros.policy.safety as safety
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.shared import TEST_SCOPE

    store = runtime_persistence.current
    store.tasks_repo.create_task(TaskRequest(id="closing", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    provider = RecordingProvider("demo", "builtin")

    token = claimed.metadata["execution_claim"]

    def close_during_safety(*_args, **_kwargs):
        import datetime

        if loss == "expired":
            from sqlalchemy import text

            with store.engine.begin() as conn:
                conn.execute(text("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id='closing'"))
            monkeypatch.setattr(
                task_transitions,
                "database_now",
                lambda conn: datetime.datetime.fromisoformat(claimed.heartbeat_at) + datetime.timedelta(seconds=120),
            )
        elif loss == "stop":
            store.tasks_repo.request_task_stop("closing", scope=TEST_SCOPE)
        elif loss == "replaced":
            claimed.metadata["execution_claim"] = "replacement"
            store.tasks_repo.upsert_task(claimed)
        else:
            store.tasks_repo.finish_detached_attempt("closing", status="completed", result="closed", claim_token=token)
        return True, ""

    monkeypatch.setattr(safety, "check_safety", close_during_safety)
    result = run_tool(
        make_catalog([provider]),
        _ctx(tmp_path, task_id="closing", detached=True, current_task_type="task", execution_claim=token),
        "demo",
        {},
    )

    assert result == "⚠️ TOOL_TASK_ATTEMPT_INACTIVE"
    assert provider.calls == []


# --- resolution order and gates ---------------------------------------------------


def test_first_provider_in_order_wins(tmp_path) -> None:
    first = RecordingProvider("demo", "builtin")
    second = RecordingProvider("demo", "skill:shadow")

    assert run_tool(make_catalog([first, second]), _ctx(tmp_path), "demo", {}) == "ran demo"
    assert len(first.calls) == 1
    assert second.calls == []


def test_unknown_tool_reports_the_local_tools(tmp_path) -> None:
    """The listing comes from local entries, so no extension liveness walk runs."""
    from new_core.tools.entry import ToolEntry
    from tests.helpers.shared import static_tool_provider

    local = static_tool_provider(
        ToolEntry(
            "demo",
            {"name": "demo", "description": "d", "parameters": {"type": "object", "properties": {}}},
            lambda ctx, **kw: "ok",
        )
    )

    result = run_tool(make_catalog([local]), _ctx(tmp_path), "nope", {})

    assert "Unknown tool: nope" in result
    assert "demo" in result


def test_a_provider_without_local_entries_still_reports_the_unknown_name(tmp_path) -> None:
    provider = RecordingProvider("demo", "builtin")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "nope", {})

    assert "Unknown tool: nope" in result
    assert provider.calls == []


# --- a name a provider owns but withholds ------------------------------------------


class WithholdingProvider(RecordingProvider):
    """Owns a name, offers nothing, and says why — the shape of a dead extension."""

    def __init__(self, name: str, refusal: str) -> None:
        super().__init__(name, "ext:demo")
        self._refusal = refusal
        self.refusals: list[str] = []

    def descriptors(self, scope):
        return []

    def get(self, name: str, scope):
        return None

    def refuse_dispatch(self, name: str, scope) -> str | None:
        if name != self._desc.name:
            return None
        self.refusals.append(name)
        return self._refusal


def test_a_withheld_name_is_explained_not_reported_as_unknown(tmp_path) -> None:
    """The regression this guards: a dead extension answered `Unknown tool`.

    Its tool is hidden from every surface read, so nothing resolves it, and dispatch used to
    conclude the name was unknown — attaching the list of *builtin* tools to a model that
    asked for an extension, and skipping the unload that stops the next task paying for the
    same dead extension again.
    """
    provider = WithholdingProvider("ext_6_r_demo_ping", "⚠️ EXTENSION_NOT_LIVE: nope")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_6_r_demo_ping", {})

    assert result == "⚠️ EXTENSION_NOT_LIVE: nope"
    assert provider.refusals == ["ext_6_r_demo_ping"]
    assert provider.calls == [], "a withheld name must not reach execute"


def test_a_withheld_name_is_explained_before_the_safety_supervisor(tmp_path, monkeypatch) -> None:
    """A call that will not run must not first cost a model call."""
    import ouroboros.policy.safety as safety

    checked: list[str] = []
    monkeypatch.setattr(safety, "check_safety", lambda name, *a, **kw: (checked.append(name), (True, ""))[1])
    provider = WithholdingProvider("ext_6_r_demo_ping", "⚠️ EXTENSION_NOT_LIVE: nope")

    run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_6_r_demo_ping", {})

    assert checked == []


def test_a_name_nobody_owns_is_still_unknown(tmp_path) -> None:
    """Asking for a refusal must not turn every typo into someone else's problem."""
    provider = WithholdingProvider("ext_6_r_demo_ping", "⚠️ EXTENSION_NOT_LIVE: nope")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "nope", {})

    assert "Unknown tool: nope" in result
    assert provider.refusals == []


def test_safety_supervisor_can_block_before_the_provider(tmp_path, monkeypatch) -> None:
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (False, "⚠️ blocked"))
    provider = RecordingProvider("demo", "builtin")

    assert run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {}) == "⚠️ blocked"
    assert provider.calls == []


def test_skips_llm_safety_bypasses_the_supervisor(tmp_path, monkeypatch) -> None:
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (False, "⚠️ blocked"))
    provider = RecordingProvider("ext_9_r_demo_own", "ext:demo", skips_llm_safety=True)

    assert run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_9_r_demo_own", {}) == "ran ext_9_r_demo_own"
    assert len(provider.calls) == 1


def test_dispatch_passes_the_descriptors_declared_policy_to_check_safety(tmp_path, monkeypatch) -> None:
    # Pins the wiring the other tests' parameter-agnostic mock cannot: the gate
    # must hand check_safety the descriptor's declared policy (the #84 contract),
    # keyed by the canonical name — reverting that call would leave this red.
    import ouroboros.policy.safety as safety

    seen: dict[str, object] = {}

    def _spy(tool_name, arguments, messages=None, ctx=None, policy=None):
        seen["tool_name"] = tool_name
        seen["policy"] = policy
        return True, ""

    monkeypatch.setattr(safety, "check_safety", _spy)
    provider = RecordingProvider("ext_9_r_demo_thing", "ext:demo", safety_policy="check")

    run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_9_r_demo_thing", {})

    assert seen["tool_name"] == "ext_9_r_demo_thing"
    assert seen["policy"] == "check"


def test_a_declared_skip_short_circuits_without_calling_check_safety(tmp_path, monkeypatch) -> None:
    # A skip verdict is immediate, so dispatch takes no thread hop for it: a
    # parallel batch of skip tools must spawn no safety executors at all.
    import ouroboros.policy.safety as safety

    called: list[str] = []
    monkeypatch.setattr(safety, "check_safety", lambda name, *a, **kw: (called.append(name), (True, ""))[1])
    provider = RecordingProvider("ext_9_r_demo_fast", "ext:demo", safety_policy="skip")

    assert run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_9_r_demo_fast", {}) == "ran ext_9_r_demo_fast"
    assert called == []


def test_check_safety_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """The LLM path of check_safety blocks; dispatch must not run it on the loop."""
    import asyncio

    import ouroboros.policy.safety as safety

    on_loop: list[bool] = []

    def _spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return True, ""

    monkeypatch.setattr(safety, "check_safety", _spy)
    provider = RecordingProvider("demo", "builtin")

    run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {})

    assert on_loop == [False], "check_safety must run in a worker thread, not on the event loop"


def test_a_warning_from_safety_is_prepended_to_the_result(tmp_path, monkeypatch) -> None:
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (True, "⚠️ careful"))
    provider = RecordingProvider("demo", "builtin")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {})

    assert result == "⚠️ careful\n\n---\nran demo"


def test_excluded_surface_is_refused_before_the_provider(tmp_path, monkeypatch) -> None:
    import ouroboros.tools.dispatch as dispatch
    from ouroboros.tools.spec import ToolSpec

    monkeypatch.setattr(
        dispatch,
        "spec_for_tool",
        lambda name: ToolSpec(name=name, surface="excluded"),
    )
    provider = RecordingProvider("demo", "builtin")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {})

    assert "TOOL_UNAVAILABLE" in result
    assert provider.calls == []


def _durable_ctx(tmp_path, task):
    return ToolContext(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        scope=TEST_SCOPE,
        skill_surface=None,
        task_id=task.task_id,
        job_id=task.job_id,
        detached=True,
        current_task_type="task",
        execution_claim=task.metadata["execution_claim"],
        through_message_id=task.input.get("through_message_id", ""),
    )


def _durable_catalog(name, handler):
    entry = ToolEntry(
        name=name, schema=ToolSchema(name=name, description=name, parameters={}), handler=handler, safety_policy="skip"
    )
    return make_catalog([static_tool_provider(entry)])


@pytest.mark.parametrize("loss", ["stop", "expired", "released"])
def test_stopped_holder_cannot_write_after_scratchpad_partition_wait(tmp_path, monkeypatch, loss):
    from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
    from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
    from ouroboros.tools.service.memory import _update_scratchpad

    store = runtime_persistence.current
    store.tasks_repo.create_task(TaskRequest(id="job", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    repo = SqlScratchpad(store.engine)
    pad = Scratchpad(repo)
    partition = repo._partition

    @contextmanager
    def delayed_partition(scope, job_id):
        # The tool passed dispatch fencing; simulate another owner stopping the
        # job while its body waits for the scratchpad partition transaction.
        if loss == "stop":
            store.tasks_repo.request_task_stop("job", scope=TEST_SCOPE)
        elif loss == "expired":
            from sqlalchemy import text

            with store.engine.begin() as conn:
                conn.execute(text("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id='job'"))
            monkeypatch.setattr(
                task_transitions,
                "database_now",
                lambda conn: datetime.datetime.fromisoformat(claimed.heartbeat_at) + datetime.timedelta(seconds=121),
            )
        else:
            result = store.tasks_repo.transition_running(
                "job",
                kind="instance_released",
                result="released",
                attempt=1,
                successor=TaskRequest(id="successor", job_id="job", detached=True, scope=TEST_SCOPE.to_dict()),
                runtime_id="runtime",
                claim_token=claimed.metadata["execution_claim"],
            )
            assert result is not None
        with partition(scope, job_id) as conn:
            yield conn

    monkeypatch.setattr(repo, "_partition", delayed_partition)
    monkeypatch.setattr("ouroboros.tools.service.memory.get_scratchpad", lambda: pad)
    result = run_tool(
        _durable_catalog("update_scratchpad", _update_scratchpad),
        _durable_ctx(tmp_path, claimed),
        "update_scratchpad",
        {"content": "notes from stopped holder"},
    )
    assert "closed" in result
    assert pad.list_blocks(TEST_SCOPE, job_id="job") == []


@pytest.mark.parametrize("successor", [False, True])
def test_detached_chat_history_tool_respects_the_admission_anchor(tmp_path, successor):
    from ouroboros.agent.context import _build_recent_chat
    from ouroboros.tools.service.memory import _chat_history

    store = runtime_persistence.current
    for message_id, content, created in [("anchor", "original request", "2026-01-01T00:00:00Z")]:
        sql_conversation_repository(store.engine).append_message(
            ConversationMessage(
                message_id=message_id, dedup_key=message_id, content=content, created_at=created, **TEST_SCOPE.to_dict()
            ),
            scope=TEST_SCOPE,
        )
    task = runtime_persistence.current.create_root_task.execute(
        TaskRequest(id="job", detached=True, text="work", scope=TEST_SCOPE.to_dict(), through_message_id="anchor"),
        enabled=True,
        max_active=5,
    )
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            message_id="future",
            dedup_key="future",
            content="POST_ADMISSION_NEW_INSTRUCTION",
            created_at="2026-01-01T00:00:01Z",
            **TEST_SCOPE.to_dict(),
        ),
        scope=TEST_SCOPE,
    )
    assert "POST_ADMISSION" not in _build_recent_chat(tmp_path, TEST_SCOPE, task.to_agent_input()).render()
    if successor:
        next_request = TaskRequest.model_validate({**claimed.input, "id": "next"})
        assert store.tasks_repo.transition_running(
            "job",
            kind="instance_released",
            result="release",
            attempt=1,
            successor=next_request,
            runtime_id="runtime",
            claim_token=claimed.metadata["execution_claim"],
        )
        claimed = store.tasks_repo.claim_next_task(runtime_id="next")
    result = run_tool(
        _durable_catalog("chat_history", _chat_history), _durable_ctx(tmp_path, claimed), "chat_history", {}
    )
    assert "POST_ADMISSION" not in result
    assert "original request" in result
    assert "POST_ADMISSION" in _chat_history(_ctx(tmp_path))


@pytest.mark.parametrize("successor", [False, True])
def test_detached_recent_tasks_tool_respects_admission(tmp_path, successor):
    from ouroboros.tools.recent_tasks import _handle_recent_tasks

    store = runtime_persistence.current
    store.tasks_repo.create_task(
        TaskRequest(id="earlier", text="PRE_ADMISSION_WORK", scope=TEST_SCOPE.to_dict()), status="completed"
    )
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(message_id="anchor", dedup_key="anchor", **TEST_SCOPE.to_dict()), scope=TEST_SCOPE
    )
    runtime_persistence.current.create_root_task.execute(
        TaskRequest(id="job", detached=True, text="work", scope=TEST_SCOPE.to_dict(), through_message_id="anchor"),
        enabled=True,
        max_active=5,
    )
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    store.tasks_repo.create_task(
        TaskRequest(id="later", text="POST_ADMISSION_OTHER_WORK", scope=TEST_SCOPE.to_dict()), status="completed"
    )
    if successor:
        next_request = TaskRequest.model_validate({**claimed.input, "id": "next"})
        assert store.tasks_repo.transition_running(
            "job",
            kind="instance_released",
            result="release",
            attempt=1,
            successor=next_request,
            runtime_id="runtime",
            claim_token=claimed.metadata["execution_claim"],
        )
        claimed = store.tasks_repo.claim_next_task(runtime_id="next")
    result = run_tool(
        _durable_catalog("recent_tasks", _handle_recent_tasks), _durable_ctx(tmp_path, claimed), "recent_tasks", {}
    )
    assert "PRE_ADMISSION_WORK" in result
    assert "POST_ADMISSION" in _handle_recent_tasks(_ctx(tmp_path))
    assert "POST_ADMISSION" not in result
