"""Tasks 6.2/6.4/6.5/6.6: the ouroboros-side task graph and the shrunk handle_task.

Covers the ``prepare_context`` / ``prepare_tools`` nodes, terminal
parameterization, a handle_task-level characterization of a
scripted task, and that cleanup + the crash path run on every exit.
"""

from __future__ import annotations

import os
import queue
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import ouroboros.agent.context as context_mod
import ouroboros.agent.core as core
import ouroboros.agent.loop_adapters as loop_adapters_mod
import ouroboros.agent.task_graph as task_graph_mod
from new_core.graph import END
from new_core.llm.types import LLMFailure, LLMMessage, ToolCall, ToolCallFunction
from new_core.loop import LoopOverrides, build_task_graph, make_run_state, nodes as loop_nodes
from new_core.loop.interfaces import LLMMiddleware
from new_core.loop.nodes import prepare_tools
from new_core.loop.routers import (
    route_after_finalize,
    route_after_recover,
    route_after_round_limit,
)
from new_core.tools.interfaces import NullToolEvents
from ouroboros.agent.task_graph import (
    build_task_run,
    resolve_task_lineage,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.events import PublishedToolEvents
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import make_mock_llm
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import (
    TEST_SCOPE,
    FakeCancellation,
    make_empty_extension_runtime,
    make_empty_skill_snapshot,
    make_empty_skill_surface,
    make_skill_availability,
    make_skill_catalog,
)
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from tests.new_core.loop._fakes import FakeLLM, FakeTools, make_config, make_ctx, make_state

# --- 6.2 head-of-graph nodes ------------------------------------------------


def test_prepare_tools_builds_the_surface_off_this_task_context():
    """The node itself: a loop-layer node reaching the surface through ``ToolRunner``."""
    tool_ctx = SimpleNamespace(events="untouched", task_id="untouched", messages="untouched")
    tools = FakeTools(overrides=LoopOverrides(pending_compaction=9), surface=(["s1"], {"tool_a"}))

    ctx = make_ctx(llm=FakeLLM([]), tools=tools)
    state = make_run_state(ctx.config, messages=[], tool_schemas=[], active_tool_names=set())
    state.task = {"id": "task-9"}
    state.messages = [{"role": "user", "content": "hi"}]
    state.tool_ctx = tool_ctx

    prepare_tools(state, ctx)

    # the surface is derived from *this* task's context, not a previous task's
    assert tools.bind_calls == [(tool_ctx, [{"role": "user", "content": "hi"}])]
    assert state.tool_schemas == ["s1"]
    assert state.active_tool_names == {"tool_a"}
    # The node writes nothing onto the context: id, events channel and transcript are all
    # bound at assembly, so the context is as opaque here as it is to the per-round nodes.
    assert (tool_ctx.events, tool_ctx.task_id, tool_ctx.messages) == ("untouched",) * 3
    # overrides seeded from the same interface, for apply_overrides in round 1
    assert state.pending_overrides.pending_compaction == 9


def test_build_tool_context_binds_the_events_channel(tmp_path):
    # The context leaves _build_tool_context complete: prepare_tools assigns nothing for
    # events, so the channel has to arrive here, bound to the run's publisher and this task.
    agent, _ = _make_agent(tmp_path)
    agent._events = RecordingAgentEvents()
    agent._current_task_type = "chat"

    ctx = agent._build_tool_context(
        {"id": "task-42"},
        None,
        root_task_id="task-42",
        scope=TEST_SCOPE,
        skill_surface=make_empty_skill_surface(),
    )

    assert isinstance(ctx.events, PublishedToolEvents)
    assert ctx.events._events is agent._events
    assert ctx.events._task_id == "task-42"
    assert ctx.events._task_type == "chat"

    ctx.events.lifecycle("skill_exec_finished", {"skill": "alpha"})
    assert agent._events.published[0].task_id == "task-42"


def test_build_tool_context_carries_the_tasks_immutable_context_boundary(tmp_path):
    agent, _ = _make_agent(tmp_path)

    ctx = agent._build_tool_context(
        {
            "id": "attempt-2",
            "job_id": "job-1",
            "detached": True,
            "inherited_context_seed": "seed",
            "committed_summary_snapshot": "summary",
            "through_message_id": "anchor",
        },
        None,
        root_task_id="attempt-2",
        scope=TEST_SCOPE,
        skill_surface=make_empty_skill_surface(),
    )

    assert (ctx.inherited_context_seed, ctx.committed_summary_snapshot, ctx.through_message_id) == (
        "seed",
        "summary",
        "anchor",
    )


def test_build_task_run_binds_the_runs_usage_totals_for_tool_side_model_calls(tmp_path):
    # A tool that calls a model outside the loop (vlm_query) reports through
    # ToolContext.events. Those tokens are the task's spend, so the sink has to accumulate
    # into the very dict `emit_result` publishes and the checkpoint shows the model --
    # otherwise the published usage silently omits it.
    agent, _tool_ctx = _make_agent(tmp_path)

    _graph_ctx, state = build_task_run(
        agent,
        {"id": "t-usage"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    ctx = agent._build_tool_context(
        {"id": "t-usage"},
        None,
        root_task_id="t-usage",
        scope=TEST_SCOPE,
        skill_surface=make_empty_skill_surface(),
    )
    ctx.events.usage("vlm/model", {"prompt_tokens": 8000, "completion_tokens": 500}, source="vlm_query")

    assert state.accumulated_usage["prompt_tokens"] == 8000
    assert state.accumulated_usage["completion_tokens"] == 500


def _run_config(agent, tmp_path, task_id: str):
    graph_ctx, _state = build_task_run(
        agent,
        {"id": task_id},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    return graph_ctx.config


def _config_of(agent, tmp_path, task: dict):
    graph_ctx, _state = build_task_run(
        agent,
        task,
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    return graph_ctx.config


_SKILL_MAP = "distribution:aifa-plan-author=planner-model@high,distribution:aifa-atom-author=coder-model"


def test_a_subtask_runs_on_the_model_its_skill_is_mapped_to(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_BY_SKILL", _SKILL_MAP)
    agent, _tool_ctx = _make_agent(tmp_path)

    planner = _config_of(
        agent, tmp_path, {"id": "t-plan", "depth": 1, "description": "Навык: distribution:aifa-plan-author. План."}
    )
    coder = _config_of(
        agent, tmp_path, {"id": "t-code", "depth": 1, "description": "Навык: distribution:aifa-atom-author. Атом."}
    )

    assert (planner.default_model, planner.default_effort) == ("planner-model", "high")
    assert coder.default_model == "coder-model"
    assert coder.default_effort == _run_config(agent, tmp_path, "t-base").default_effort


def test_the_root_and_unmapped_subtasks_keep_the_owners_main_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_BY_SKILL", _SKILL_MAP)
    agent, _tool_ctx = _make_agent(tmp_path)
    main = _run_config(agent, tmp_path, "t-main").default_model

    # A user message that starts like a skill order is still the root: no model switch.
    root = _config_of(agent, tmp_path, {"id": "t-root", "depth": 0, "text": "Навык: distribution:aifa-plan-author. Х"})
    root_described = _config_of(
        agent, tmp_path, {"id": "t-root2", "depth": 0, "description": "Навык: distribution:aifa-plan-author. Х"}
    )
    other = _config_of(agent, tmp_path, {"id": "t-o", "depth": 1, "description": "Навык: distribution:other. Х"})

    assert {root.default_model, root_described.default_model, other.default_model} == {main}


def test_build_task_run_permits_the_narration_by_default(tmp_path):
    agent, _tool_ctx = _make_agent(tmp_path)

    assert _run_config(agent, tmp_path, "t-loud").reasoning_progress is True


def test_build_task_run_reads_its_settings_for_the_runs_own_scope(tmp_path):
    from config_registry.user_settings.reader import REASONING_PROGRESS_KEY
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    overrides = {
        REASONING_PROGRESS_KEY: "false",
        "OUROBOROS_EFFORT_TASK": "high",
        "OUROBOROS_MODEL_FALLBACK": "fallback-of-this-owner",
    }
    reset_user_settings_for_tests(StubUserSettingsStore(overrides))
    agent, _tool_ctx = _make_agent(tmp_path)

    config = _run_config(agent, tmp_path, "t-quiet")

    assert config.reasoning_progress is False
    assert config.default_effort == "high"
    assert config.fallback_model == "fallback-of-this-owner"


def test_a_task_sees_an_override_written_since_the_previous_task_read_it(tmp_path):
    """No process-lifetime cache: the writer is another tier, so the next task reads the store."""
    from config_registry.user_settings.reader import REASONING_PROGRESS_KEY
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    store = StubUserSettingsStore()
    reset_user_settings_for_tests(store)
    agent, _tool_ctx = _make_agent(tmp_path)
    assert _run_config(agent, tmp_path, "t-first").reasoning_progress is True

    store.put(TEST_SCOPE.owner_id, TEST_SCOPE.agent_id, REASONING_PROGRESS_KEY, "false")

    assert _run_config(agent, tmp_path, "t-second").reasoning_progress is False


def test_build_task_run_owns_per_task_tool_execution_state(tmp_path):
    # The ledger and the repeat guard live on the adapters, not on ToolContext.
    # The ledger has two users — the tool runner writes it, the compactor renders
    # it — so they must get the very same instance; both objects are per task.
    agent, _tool_ctx = _make_agent(tmp_path)

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-ledger"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert isinstance(graph_ctx.tools._executor._task_ledger, TaskLedger)
    assert graph_ctx.tools._executor._task_ledger is graph_ctx.compactor._task_ledger
    # The guard has a single owner — the executor — so no second reference can drift.
    assert isinstance(graph_ctx.tools._executor._repeat_guard, RepeatToolGuard)
    assert not hasattr(graph_ctx.tools, "_repeat_guard")
    # One sink for the run: tool live logs and the graph's own events go out the same channel.
    assert graph_ctx.tools._executor._events is graph_ctx.events

    other_ctx, _ = build_task_run(
        agent,
        {"id": "t-other"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    assert other_ctx.tools._executor._task_ledger is not graph_ctx.tools._executor._task_ledger
    assert other_ctx.tools._executor._repeat_guard is not graph_ctx.tools._executor._repeat_guard


def test_build_task_run_gives_each_task_its_own_llm_middlewares(tmp_path, monkeypatch):
    # A middleware keeps per-task state (what it has already checked), and tasks run on
    # worker threads: the security_config list holds factories, called once per task.
    class _Middleware(LLMMiddleware):
        def before_call(self, messages):
            pass

        def after_call(self, response):
            pass

    monkeypatch.setattr(task_graph_mod, "llm_middlewares", [_Middleware])
    agent, _tool_ctx = _make_agent(tmp_path)

    first, _ = build_task_run(
        agent,
        {"id": "t-1"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    second, _ = build_task_run(
        agent,
        {"id": "t-2"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    (first_middleware,) = first.llm.middlewares
    (second_middleware,) = second.llm.middlewares
    assert isinstance(first_middleware, _Middleware)
    assert first_middleware is not second_middleware


def test_build_task_run_uses_the_one_process_tool_catalog(tmp_path):
    """Only the skill surface is per-task; tools remain process-wide.

    It used to be rebuilt for every task, which re-walked the skills tree and executed
    every `type: tool` module again. Refreshing the shared one buys the same freshness —
    a skill enabled since the last task shows up — without a second catalog existing.
    """
    from new_core.tools.catalog import tool_catalog

    agent, _tool_ctx = _make_agent(tmp_path)

    first, _ = build_task_run(
        agent,
        {"id": "t-1"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    second, _ = build_task_run(
        agent,
        {"id": "t-2"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert first.tools._executor.catalog is tool_catalog
    assert second.tools._executor.catalog is tool_catalog


def test_build_task_run_only_captures_the_composed_skill_snapshot(tmp_path):
    agent, _tool_ctx = _make_agent(tmp_path)
    _give_agent_skills(agent, tmp_path, "alpha")
    late_skill = tmp_path / "skills" / "late"
    late_skill.mkdir()
    (late_skill / "SKILL.md").write_text(
        "---\nname: late\ndescription: late\ntype: instruction\n---\n# late\n",
        encoding="utf-8",
    )

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-frozen-skills"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert {skill.name for skill in graph_ctx.assembler._skill_surface.skills()} == {"alpha"}


@pytest.mark.usefixtures("multiuser_mode")
@parametrize_from_dict({"multiuser": {"multiuser": True}, "single-user": {"multiuser": False}})
def test_build_task_run_applies_the_owner_skill_allowlist_only_in_multiuser(tmp_path, multiuser, monkeypatch):
    from config_registry.user_settings.reader import USER_ENABLED_SKILL_IDS_KEY
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    class RecordingStore(StubUserSettingsStore):
        def __init__(self):
            super().__init__(
                by_pair={
                    (TEST_SCOPE.owner_id, TEST_SCOPE.agent_id): {
                        USER_ENABLED_SKILL_IDS_KEY: '["distribution:alpha", "distribution:alpha"]'
                    },
                    ("other-owner", TEST_SCOPE.agent_id): {USER_ENABLED_SKILL_IDS_KEY: '["distribution:beta"]'},
                }
            )
            self.requests: list[tuple[str, str, str]] = []

        def get_setting_value(self, owner_id, agent_id, key):
            self.requests.append((owner_id, agent_id, key))
            return super().get_setting_value(owner_id, agent_id, key)

    monkeypatch.setenv("OUROBOROS_MULTIUSER", str(multiuser).lower())
    store = RecordingStore()
    reset_user_settings_for_tests(store)
    agent, _tool_ctx = _make_agent(tmp_path)
    _give_agent_skills(agent, tmp_path, "alpha", "beta")
    agent.skill_availability = make_skill_availability(executable_skills=not multiuser)

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-selected"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    surface = graph_ctx.assembler._skill_surface
    expected = {"distribution:alpha"} if multiuser else {"distribution:alpha", "distribution:beta"}
    assert {str(skill.skill_id) for skill in surface.skills()} == expected
    assert graph_ctx.assembler._skill_origins == (frozenset({"skill:alpha"}) if multiuser else None)
    assert store.requests.count((TEST_SCOPE.owner_id, TEST_SCOPE.agent_id, USER_ENABLED_SKILL_IDS_KEY)) == int(
        multiuser
    )


@pytest.mark.usefixtures("multiuser_mode")
def test_skill_allowlist_change_affects_the_next_task_and_not_the_active_surface(tmp_path):
    from config_registry.user_settings.reader import USER_ENABLED_SKILL_IDS_KEY
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    store = StubUserSettingsStore({USER_ENABLED_SKILL_IDS_KEY: "distribution:alpha"})
    reset_user_settings_for_tests(store)
    agent, _tool_ctx = _make_agent(tmp_path)
    _give_agent_skills(agent, tmp_path, "alpha", "beta")
    agent.skill_availability = make_skill_availability(executable_skills=False)

    first, _ = build_task_run(
        agent,
        {"id": "t-first"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    store.put(TEST_SCOPE.owner_id, TEST_SCOPE.agent_id, USER_ENABLED_SKILL_IDS_KEY, "distribution:beta")
    second, _ = build_task_run(
        agent,
        {"id": "t-second"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    first_surface = first.assembler._skill_surface
    second_surface = second.assembler._skill_surface
    assert {str(skill.skill_id) for skill in first_surface.skills()} == {"distribution:alpha"}
    assert {str(skill.skill_id) for skill in second_surface.skills()} == {"distribution:beta"}
    assert first.assembler._skill_origins == frozenset({"skill:alpha"})
    assert second.assembler._skill_origins == frozenset({"skill:beta"})
    first_ctx = first.assembler.tool_context(task={"id": "t-first"}, task_workspace=None, scope=TEST_SCOPE, messages=[])
    recovered_ctx = second.assembler.tool_context(
        task={"id": "t-first"}, task_workspace=None, scope=TEST_SCOPE, messages=[]
    )
    context_mod.restore_skills(recovered_ctx, context_mod.skills_snapshot(first_ctx))
    assert recovered_ctx.skill_origins == frozenset({"skill:alpha"})
    assert recovered_ctx.skill_surface.admitted_ids() == first_surface.admitted_ids()
    assert {str(skill.skill_id) for skill in agent.skill_snapshot.skills()} == {
        "distribution:alpha",
        "distribution:beta",
    }


@pytest.mark.usefixtures("multiuser_mode")
def test_skill_allowlist_read_failure_denies_skills_without_removing_other_tools(tmp_path):
    from config_registry.user_settings.reader import USER_ENABLED_SKILL_IDS_KEY
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    class FailingSkillSelectionStore(StubUserSettingsStore):
        def get_setting_value(self, owner_id, agent_id, key):
            if key == USER_ENABLED_SKILL_IDS_KEY:
                raise RuntimeError("allowlist store unavailable")
            return super().get_setting_value(owner_id, agent_id, key)

    reset_user_settings_for_tests(FailingSkillSelectionStore())
    agent, _tool_ctx = _make_agent(tmp_path)
    _give_agent_skills(agent, tmp_path, "alpha", "beta")
    agent.skill_availability = make_skill_availability(executable_skills=False)

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-denied"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert graph_ctx.assembler._skill_surface.skills() == ()
    assert graph_ctx.tools._executor.catalog is task_graph_mod.tool_catalog


@pytest.mark.usefixtures("multiuser_mode")
def test_missing_skill_allowlist_uses_the_empty_instance_default(tmp_path):
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    reset_user_settings_for_tests(StubUserSettingsStore())
    agent, _tool_ctx = _make_agent(tmp_path)
    _give_agent_skills(agent, tmp_path, "alpha", "beta")
    agent.skill_availability = make_skill_availability(executable_skills=False)

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-default-denied"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert graph_ctx.assembler._skill_surface.skills() == ()


@pytest.mark.usefixtures("multiuser_mode")
def test_missing_skill_allowlist_uses_the_instance_enabled_ids(tmp_path, monkeypatch):
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    monkeypatch.setenv("OUROBOROS_USER_ENABLED_SKILL_IDS", '["distribution:beta"]')
    reset_user_settings_for_tests(StubUserSettingsStore())
    agent, _tool_ctx = _make_agent(tmp_path)
    _give_agent_skills(agent, tmp_path, "alpha", "beta")
    agent.skill_availability = make_skill_availability(executable_skills=False)

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-instance-allowlist"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert {str(skill.skill_id) for skill in graph_ctx.assembler._skill_surface.skills()} == {"distribution:beta"}


@pytest.mark.usefixtures("multiuser_mode")
def test_malformed_skill_allowlist_denies_every_skill(tmp_path):
    from config_registry.user_settings.reader import USER_ENABLED_SKILL_IDS_KEY
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    reset_user_settings_for_tests(StubUserSettingsStore({USER_ENABLED_SKILL_IDS_KEY: "legacy-name"}))
    agent, _tool_ctx = _make_agent(tmp_path)
    _give_agent_skills(agent, tmp_path, "alpha", "beta")
    agent.skill_availability = make_skill_availability(executable_skills=False)

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-malformed"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert graph_ctx.assembler._skill_surface.skills() == ()


# --- head-of-graph event order + lineage ------------------------------------


def test_the_head_emits_typing_the_record_then_the_context_bracket(tmp_path, monkeypatch):
    """The order one node used to guarantee, now guaranteed by two (D6).

    ``typing`` leads, then the task record is opened, then context assembly brackets
    itself with its live logs. Splitting the old ``_prepare_task_context`` is only
    behavior-preserving if this sequence is unchanged.
    """
    from ouroboros.storage.task_results import STATUS_RUNNING

    agent, _ = _make_agent(tmp_path)
    order: list[str] = []

    monkeypatch.setattr(agent, "_emit_live_log", lambda _scope, name, **_kw: order.append(name))
    monkeypatch.setattr(
        loop_adapters_mod, "record_event", lambda _stream, name, _payload, **_kwargs: order.append(name)
    )
    monkeypatch.setattr(
        loop_adapters_mod, "write_task_result", lambda _root, _task_id, status, **_kw: order.append(status)
    )
    monkeypatch.setattr(loop_adapters_mod.EventSinkAdapter, "typing", lambda _self: order.append("typing"))

    graph_ctx, state = build_task_run(
        agent,
        {"id": "t-order", "type": "chat", "text": "hi"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path / "logs",
        cancellation=FakeCancellation(),
    )
    loop_nodes.record_task_started(state, graph_ctx)
    loop_nodes.prepare_context(state, graph_ctx)
    loop_nodes.open_transcript(state, graph_ctx)

    assert order == [
        "typing",
        "task_received",
        STATUS_RUNNING,
        "context_building_started",
        "context_building_finished",
    ]


def test_the_task_record_is_read_once_for_the_whole_run(tmp_path, monkeypatch):
    """Lineage is one durable read, shared by the record write and the built context (D4)."""
    agent, _ = _make_agent(tmp_path)
    reads: list[str] = []

    monkeypatch.setattr(agent, "_emit_live_log", lambda *_a, **_kw: None)
    monkeypatch.setattr(loop_adapters_mod, "record_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(loop_adapters_mod, "write_task_result", lambda *_a, **_kw: None)
    monkeypatch.setattr(task_graph_mod, "load_task_result", lambda _root, task_id: reads.append(task_id) or {})

    graph_ctx, state = build_task_run(
        agent,
        {"id": "t-lineage", "type": "chat", "text": "hi", "parent_task_id": "p-1"},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path / "logs",
        cancellation=FakeCancellation(),
    )
    loop_nodes.record_task_started(state, graph_ctx)
    loop_nodes.prepare_context(state, graph_ctx)

    assert reads == ["t-lineage"]
    # Both halves got the same resolved lineage, without deriving it themselves.
    assert graph_ctx.events._parent_task_id == "p-1"
    assert graph_ctx.events._root_task_id == "p-1"
    assert graph_ctx.assembler._root_task_id == "p-1"
    assert state.tool_ctx.root_task_id == "p-1"


def test_resolve_task_lineage_roots_on_the_task_itself_without_a_record(tmp_path):
    agent, _ = _make_agent(tmp_path)

    parent, root = resolve_task_lineage(agent.env.drive_root, {"id": "solo"})

    assert (parent, root) == (None, "solo")


# --- 6.4 task-graph topology + terminal parameterization -------------------


def test_terminal_routes_to_end_standalone_and_emit_when_embedded():
    # Standalone loop: terminal defaults to END.
    ctx_end = make_ctx(llm=FakeLLM([]))
    s = make_state(ctx_end)
    s.final_content = "done"
    assert route_after_finalize(s, ctx_end) is END
    s.last_resp = None
    assert route_after_recover(s, ctx_end) is END
    assert route_after_round_limit(s, ctx_end) is END

    # Embedded task graph: terminal is the emit node.
    ctx_emit = make_ctx(llm=FakeLLM([]), config=make_config(terminal="emit"))
    s2 = make_state(ctx_emit)
    s2.final_content = "done"
    assert route_after_finalize(s2, ctx_emit) == "emit"
    s2.last_resp = None
    assert route_after_recover(s2, ctx_emit) == "emit"
    assert route_after_round_limit(s2, ctx_emit) == "emit"


# --- 6.5 / 6.6 handle_task characterization + cleanup/crash ----------------


def _make_agent(tmp_path):
    agent = object.__new__(core.OuroborosAgent)
    agent.session_repository = sql_owner_session_repository()
    agent.execution_memory_repository = sql_execution_memory_repository()
    agent.audit_event_repository = sql_audit_event_repository()
    agent.tool_confirmation_reader = sql_tool_confirmation_reader()
    agent.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    agent.skill_snapshot = make_empty_skill_snapshot()
    agent.skill_availability = make_skill_availability()
    agent.extension_runtime = make_empty_extension_runtime()
    agent._pending_events = []
    agent._events = None
    agent._current_task_type = None
    agent._current_task_id = None
    agent._run_state = None
    agent._task_usage = {}
    agent._outbound = None
    agent._incoming_messages = queue.Queue()
    agent._last_progress_ts = 0.0
    agent._task_started_ts = 0.0
    agent.llm = make_mock_llm(model="m1")
    tool_ctx = SimpleNamespace(
        _pending_compaction=None,
        pending_skill_read=None,
        projected_skills=set(),
        events=NullToolEvents(),
        task_id=None,
        messages=None,
        scope=TEST_SCOPE,
        # These tests patch out bind_tool_surface, so nothing installs a surface;
        # drain_overrides reads the attribute unconditionally and tolerates None.
        tool_surface=None,
    )
    agent._task_graph = build_task_graph()
    return agent, tool_ctx


def _give_agent_skills(agent, root, *names: str) -> None:
    for name in names:
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\ntype: instruction\n---\n# {name}\n",
            encoding="utf-8",
        )
    agent.skill_snapshot = make_skill_catalog(repo_dir=root, drive_root=root).snapshot()


@pytest.mark.parametrize(
    "boundary",
    [
        "guard",
        "task_started",
        "model_admission",
        "round_limit",
        "language_repair",
        "tool",
        "tool_sql",
        "tool_io",
        "context_io",
        "history_read",
        "history_sql",
        "runtime_io",
        "result_read",
        "result_write",
        "commit_unknown",
        "failure_write",
        "failure_commit_unknown",
    ],
)
def test_detached_execution_failures_preserve_the_correct_recovery_path(tmp_path, monkeypatch, boundary):
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from ouroboros.agent_events.interfaces import OutboundText, TaskFinished
    from ouroboros.llm.client import _llm_concurrency_slot
    from ouroboros.storage.runtime_models import ConversationMessage, TaskRequest
    from tests.helpers.executor import make_executor
    from tests.helpers.providers import RecordingProvider
    from tests.helpers.shared import make_catalog, make_tool_ctx

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(message_id="anchor", dedup_key="anchor", **TEST_SCOPE.to_dict()), scope=TEST_SCOPE
    )
    store.tasks_repo.create_task(
        TaskRequest(
            id="claim-error", detached=True, text="work", scope=TEST_SCOPE.to_dict(), through_message_id="anchor"
        )
    )
    executor = make_executor(tmp_path, store)
    agent, _ = _make_agent(tmp_path)
    tool_ctx = make_tool_ctx(
        repo_dir=tmp_path, drive_root=tmp_path, task_id="claim-error", detached=True, current_task_type="task"
    )
    monkeypatch.setattr(task_graph_mod, "bind_tool_surface", lambda *a: ([], {"noop"}))
    monkeypatch.setattr(executor, "new_agent", lambda: agent)
    monkeypatch.setattr(
        agent,
        "_start_task_heartbeat_loop",
        lambda *a, **kw: (
            monkeypatch.setattr(store.tasks_repo, "is_task_attempt_active", fail_once)
            if boundary == "task_started"
            else None
        ),
    )
    active = store.tasks_repo.is_task_attempt_active

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(store.tasks_repo, "is_task_attempt_active", active)
        raise OSError("claim database unavailable")

    dispatched = []
    provider = RecordingProvider("noop", "test", safety_policy="skip")
    monkeypatch.setattr(task_graph_mod, "tool_catalog", make_catalog([provider]))
    finish = store.tasks_repo.finish_detached_attempt
    get_task = store.tasks_repo.get_task

    def result_failure(*args, **kwargs):
        monkeypatch.setattr(store.tasks_repo, "finish_detached_attempt", finish)
        monkeypatch.setattr(store.tasks_repo, "get_task", get_task)
        if boundary in {"commit_unknown", "failure_commit_unknown"}:
            finish(*args, **kwargs)
        raise OSError("result database unavailable")

    def tool_failure(*args, **kwargs):
        provider.calls.append((args, kwargs))
        raise OSError("ordinary tool I/O failed")

    if boundary == "tool_io":
        monkeypatch.setattr(provider, "execute", tool_failure)
    if boundary == "tool_sql":

        def fail_once(*args, **kwargs):
            monkeypatch.setattr(store.tasks_repo, "is_task_attempt_active", active)
            raise SQLAlchemyError("claim database unavailable")

    def chat(**kwargs):
        if boundary in {"tool", "tool_sql", "tool_io"} and not dispatched:
            dispatched.append(True)
            if boundary != "tool_io":
                monkeypatch.setattr(store.tasks_repo, "is_task_attempt_active", fail_once)
            return {
                "content": "Выполняю",
                "tool_calls": [ToolCall(id="noop", function=ToolCallFunction(name="noop", arguments="{}")).to_dict()],
            }, {}
        if boundary in {"result_read", "result_write", "commit_unknown", "tool", "tool_sql", "tool_io"}:
            dispatched.append(True)
            if boundary in {"result_read", "result_write", "commit_unknown"}:
                monkeypatch.setattr(
                    store.tasks_repo,
                    "get_task" if boundary == "result_read" else "finish_detached_attempt",
                    result_failure,
                )
            return {"content": "Готово"}, {}

        if boundary in {"round_limit", "language_repair"} and not dispatched:
            dispatched.append(True)
            calls = [ToolCall(id="noop", function=ToolCallFunction(name="noop", arguments="{}"))]
            return {
                "content": "漢字",
                "tool_calls": [call.to_dict() for call in calls] if boundary == "round_limit" else None,
            }, {}
        monkeypatch.setattr(store.tasks_repo, "is_task_attempt_active", fail_once)
        with _llm_concurrency_slot():
            dispatched.append(True)
            return {"content": "done"}, {}

    def build_tool_context(task, *args, **kwargs):
        tool_ctx.execution_claim = task["execution_claim"]
        if boundary in {"failure_write", "failure_commit_unknown"}:
            monkeypatch.setattr(store.tasks_repo, "finish_detached_attempt", result_failure)
        if boundary in {"context_io", "failure_write", "failure_commit_unknown"}:
            raise OSError("ordinary context file failed")
        if boundary == "guard":
            monkeypatch.setattr(store.tasks_repo, "is_task_attempt_active", fail_once)
        return tool_ctx

    agent.llm.chat.side_effect = chat
    if boundary == "round_limit":
        monkeypatch.setattr(loop_adapters_mod.ToolRunnerAdapter, "handle_tool_calls", lambda *a, **kw: [])
        monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "1")
        from tests.conftest import refresh_test_config

        refresh_test_config()

    if boundary in {"history_read", "history_sql"}:
        history_read = sql_conversation_repository(store.engine).list_chat_history_messages
        history_failures = []

        def history_failure(**kwargs):
            history_failures.append(True)
            monkeypatch.setattr(
                runtime_persistence.current.conversations_repo, "list_chat_history_messages", history_read
            )
            failure = SQLAlchemyError if boundary == "history_sql" else OSError
            raise failure("history database unavailable")

        monkeypatch.setattr(
            runtime_persistence.current.conversations_repo, "list_chat_history_messages", history_failure
        )
    else:
        monkeypatch.setattr(agent, "_build_tool_context", build_tool_context)
        monkeypatch.setattr(
            agent,
            "_assemble_opening_transcript",
            lambda *a, **_kwargs: [{"role": "user", "content": "Ответь на русском"}],
        )
    if boundary == "runtime_io":

        def new_agent():
            raise OSError("ordinary agent startup file failed")

        monkeypatch.setattr(executor, "new_agent", new_agent)
    try:
        executor.assign_next()
        executor.all_slots()[0].future.result(timeout=15)
        if boundary in {"history_read", "history_sql"}:
            assert history_failures == [True]
            agent.llm.chat.assert_not_called()
        expected_calls = (
            2
            if boundary == "tool_io"
            else int(
                boundary
                in {
                    "round_limit",
                    "language_repair",
                    "tool",
                    "tool_sql",
                    "result_read",
                    "result_write",
                    "commit_unknown",
                }
            )
        )
        assert len(dispatched) == expected_calls
        assert len(provider.calls) == int(boundary == "tool_io")
        record = store.tasks_repo.get_task("claim-error")
        terminal = {
            "context_io": "failed",
            "runtime_io": "failed",
            "failure_commit_unknown": "failed",
            "commit_unknown": "completed",
            "tool_io": "completed",
        }.get(boundary)
        assert record.status == (terminal or "running") and record.stop_requested_at is None
        if boundary in {"commit_unknown", "failure_commit_unknown"}:
            assert not any(isinstance(event, (OutboundText, TaskFinished)) for event in executor._events.published)
        if terminal:
            assert executor.task_queue.reclaim_stale_leases() == []
            assert store.tasks_repo.get_current_job(record.job_id, scope=TEST_SCOPE).task_id == record.task_id
            return
        assert not any(isinstance(event, (OutboundText, TaskFinished)) for event in executor._events.published)
        with store.engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id='claim-error'"))
        executor.task_queue.reclaim_stale_leases()
        successor = store.tasks_repo.get_current_job("claim-error", scope=TEST_SCOPE)
        assert successor.task_id != record.task_id and successor.status == "queued"
    finally:
        executor.shutdown()


def _drive_handle_task(
    tmp_path,
    responses,
    *,
    handle_tool_calls=None,
    order=None,
    expect_raise=None,
    preseed_incoming=None,
    task_overrides=None,
):
    agent, tool_ctx = _make_agent(tmp_path)
    for item in preseed_incoming or []:
        agent._incoming_messages.put(item)
    published: dict = {}
    cleaned = {"browser": [], "mailbox": [], "tmp": []}
    typing_stops = []
    agent._emit_typing_stop = typing_stops.append
    calls = {"n": 0}

    agent._build_tool_context = lambda task, workspace, **kwargs: tool_ctx
    agent._assemble_opening_transcript = lambda *a, **_kwargs: [{"role": "user", "content": "go"}]
    agent._emit_live_log = lambda *a, **k: None

    def fake_call(
        _llm,
        msgs,
        model,
        tools,
        effort,
        max_retries,
        drive_logs,
        task_id,
        round_idx,
        events,
        usage,
        task_type,
        use_local=False,
        scope=None,
        cancellation=None,
        audit_event_repository=None,
    ):
        if order is not None:
            order.append("llm")
        usage["rounds"] = usage.get("rounds", 0) + 1
        resp = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return resp

    def default_tools(*, tool_calls, **kwargs):
        if order is not None:
            order.append("tools")
        return [{"tool_call_id": tc.id, "tool": tc.function.name} for tc in tool_calls]

    def default_results(*, results, messages, trace, **kwargs):
        for result in results:
            messages.append({"role": "tool", "tool_call_id": result["tool_call_id"], "content": "result"})
            trace["tool_calls"].append({"tool": result["tool"]})
        return 0

    def spy_emit(env, llm, pending_events, task, text, usage, trace, start_time, drive_logs, ctx=None, **kwargs):
        published.update(text=text, usage=dict(usage), trace=trace, ctx=ctx, kwargs=dict(kwargs))
        pending_events.append({"type": "published", "text": text})

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=fake_call),
        patch.object(
            loop_adapters_mod.ToolRunnerAdapter, "handle_tool_calls", side_effect=handle_tool_calls or default_tools
        ),
        patch.object(loop_adapters_mod.ToolRunnerAdapter, "process_tool_results", side_effect=default_results),
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "upsert_ledger_note", return_value=None),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(task_graph_mod, "bind_tool_surface", return_value=([], set())),
        patch("ouroboros.agent.task_pipeline.emit_task_results", side_effect=spy_emit),
        patch("ouroboros.tools.browser.cleanup_browser", side_effect=lambda ctx: cleaned["browser"].append(ctx)),
        patch(
            "ouroboros.agent.owner_inject.cleanup_task_mailbox",
            side_effect=lambda *a, **k: cleaned["mailbox"].append(1),
        ),
        patch(
            "ouroboros.agent.tool_receipts.cleanup_tmp_tool_artifacts",
            side_effect=lambda *a, **k: cleaned["tmp"].append(1),
        ),
    ):
        task = {
            "id": "task-1",
            "type": "chat",
            "text": "go",
            "chat_id": 0,
            "source": "browser",
            "scope": TEST_SCOPE.to_dict(),
        }
        task.update(task_overrides or {})
        if task.get("detached"):
            from ouroboros.storage.runtime_models import TaskRequest
            from ouroboros.storage.runtime_persistence import runtime_persistence

            store = runtime_persistence.current
            store.tasks_repo.create_task(TaskRequest.model_validate(task))
            task = store.tasks_repo.claim_next_task(runtime_id="test").to_agent_input()
        if expect_raise is not None:
            with pytest.raises(expect_raise):
                agent.handle_task(task, cancellation=FakeCancellation())
            events = None
        else:
            events = agent.handle_task(task, cancellation=FakeCancellation())

    return SimpleNamespace(
        agent=agent, tool_ctx=tool_ctx, events=events, published=published, cleaned=cleaned, typing_stops=typing_stops
    )


def test_handle_task_characterization_final_text_usage_trace():
    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    responses = [
        LLMMessage(content="working on it", tool_calls=[tc]),
        LLMMessage(content="final answer", tool_calls=None),
    ]
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        import pathlib

        res = _drive_handle_task(pathlib.Path(d), responses)

    assert res.published["text"] == "final answer"
    assert res.published["usage"]["rounds"] == 2
    assert "working on it" in res.published["trace"]["reasoning_notes"]
    assert "final answer" in res.published["trace"]["reasoning_notes"]
    assert [c["tool"] for c in res.published["trace"]["tool_calls"]] == ["noop"]
    # handle_task returns the pending-events list, now filled by the emit node.
    assert res.events == [{"type": "published", "text": "final answer"}]


def test_handle_task_per_round_ordering():
    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    responses = [
        LLMMessage(content="", tool_calls=[tc]),
        LLMMessage(content="done", tool_calls=None),
    ]
    order: list[str] = []
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        _drive_handle_task(pathlib.Path(d), responses, order=order)

    # prepare_context/prepare_tools (drain) then two rounds; round 1 ends in tools,
    # round 2 finalizes.
    assert order == ["llm", "tools", "llm"]


def test_handle_task_empty_response_uses_fallback():
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        res = _drive_handle_task(pathlib.Path(d), [LLMMessage(content="", tool_calls=None)])
    assert res.published["text"] == "⚠️ Model returned an empty response. Try rephrasing your request."


def _assert_clean_exit(res):
    assert res.cleaned["browser"] == [res.tool_ctx]
    assert res.cleaned["mailbox"] == [1]
    assert res.cleaned["tmp"] == [1]
    # per-task fields reset, identity included: an idle agent holds no run state
    assert res.agent._current_task_type is None
    assert res.agent._current_task_id is None
    assert res.agent._run_state is None


def test_cleanup_on_normal_exit(tmp_path):
    res = _drive_handle_task(tmp_path, [LLMMessage(content="done", tool_calls=None)])
    _assert_clean_exit(res)
    # temporary workspace removed
    assert not list((tmp_path / "tmp").glob("ouroboros-task-*"))


def test_cleanup_with_a_message_left_in_the_owner_inbox(tmp_path):
    """Nothing puts into that queue from the supervisor any more; teardown is unaffected."""
    res = _drive_handle_task(
        tmp_path,
        [LLMMessage(content="done", tool_calls=None)],
        preseed_incoming=["leftover owner message"],
    )
    _assert_clean_exit(res)


def test_cleanup_on_round_limit(tmp_path):
    from tests.conftest import refresh_test_config

    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    with patch.dict(os.environ, {"OUROBOROS_MAX_ROUNDS": "1"}, clear=False):
        refresh_test_config()
        res = _drive_handle_task(
            tmp_path,
            [LLMMessage(content="", tool_calls=[tc]), LLMMessage(content="wrapped", tool_calls=None)],
        )
    assert res.published["text"] == "wrapped"
    _assert_clean_exit(res)


def test_cleanup_on_exhausted_recovery(tmp_path):
    res = _drive_handle_task(tmp_path, [LLMFailure("empty response"), LLMFailure("empty response")])
    assert res.published["text"].startswith("⚠️")


def test_detached_handle_emits_no_typing_stop(tmp_path):
    res = _drive_handle_task(
        tmp_path, [LLMMessage(content="done", tool_calls=None)], task_overrides={"detached": True, "job_id": "job-1"}
    )

    assert res.typing_stops == []
    _assert_clean_exit(res)


def test_crash_path_writes_failed_and_publishes_error(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("tool exploded")

    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    res = _drive_handle_task(
        tmp_path,
        [LLMMessage(content="", tool_calls=[tc])],
        handle_tool_calls=boom,
    )
    # The node exception is caught: FAILED result written, error text published
    # via the same emit path, and handle_task returns instead of re-raising.
    assert res.published["text"].startswith("⚠️ Error during processing: RuntimeError")
    assert res.events == [{"type": "published", "text": res.published["text"]}]
    from ouroboros.storage.task_results import STATUS_FAILED, load_task_result

    stored = load_task_result(tmp_path, "task-1") or {}
    assert stored.get("status") == STATUS_FAILED
    # Nobody stopped this task, so the publication must not read as a late answer to a stop.
    assert res.published["kwargs"]["stopped_externally"] is False
    _assert_clean_exit(res)


def test_crash_after_an_external_stop_keeps_the_record_and_the_late_label(tmp_path):
    """A record closed by the supervisor is not re-failed here, and its answer stays late."""
    from ouroboros.storage.task_results import STATUS_FAILED, load_task_result, write_task_result

    write_task_result(
        tmp_path, "task-1", STATUS_FAILED, scope=TEST_SCOPE, result="Task killed by hard timeout after 1800s."
    )

    def boom(*_a, **_k):
        raise RuntimeError("tool exploded")

    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    res = _drive_handle_task(tmp_path, [LLMMessage(content="", tool_calls=[tc])], handle_tool_calls=boom)

    assert res.published["kwargs"]["stopped_externally"] is True
    stored = load_task_result(tmp_path, "task-1") or {}
    assert stored.get("status") == STATUS_FAILED
    assert stored.get("result") == "Task killed by hard timeout after 1800s."


# --- the operation identifier is released on every exit ----------------------


def _trace_id_during_the_task() -> tuple[list[str], object]:
    """Observe the identifier from inside the task: `agent_start` opens right after it is set."""
    seen: list[str] = []

    @contextmanager
    def recording_agent_start(*, input):
        del input
        seen.append(core.aef_tracing.current_trace_id())
        yield SimpleNamespace(add_output_result=lambda **_kwargs: None)

    return seen, patch.object(core.aef_tracing, "agent_start", recording_agent_start)


def test_the_identifier_does_not_outlive_a_completed_task(tmp_path):
    """The agent thread is long-lived and runs tasks in sequence: an identifier left established
    labels every later log record and outbound header with an operation that already ended."""
    seen, recording = _trace_id_during_the_task()

    with recording:
        _drive_handle_task(tmp_path, [LLMMessage(content="done", tool_calls=None)])

    assert seen and seen[0]
    assert core.aef_tracing.current_trace_id() == ""


def test_the_identifier_does_not_outlive_a_task_that_fails_before_its_span(tmp_path):
    """The setup above the span is the gap the seeding set used to leave open: it established the
    identifier, and the only reset lived in a `finally` this path never reaches."""
    agent, _tool_ctx = _make_agent(tmp_path)
    task = {"id": "task-1", "type": "chat", "text": "go", "chat_id": 0, "scope": TEST_SCOPE.to_dict()}

    with patch.object(core, "_task_scope", side_effect=RuntimeError("scope is unparseable")):
        with pytest.raises(RuntimeError):
            agent.handle_task(task, cancellation=FakeCancellation())

    assert core.aef_tracing.current_trace_id() == ""


def test_the_agent_holds_no_identity_field_beside_the_run_state(tmp_path):
    """And exposes no accessor onto it either: the supervisor addresses records, not agents."""
    agent, _ = _make_agent(tmp_path)

    for removed in ("_current_chat_id", "_current_task_source", "_current_transport", "_busy"):
        assert not hasattr(agent, removed), removed
    for removed in ("active_scope", "active_participant"):
        assert not hasattr(type(agent), removed), removed


def test_a_task_without_a_scope_fails_descriptively(tmp_path):
    agent, _ = _make_agent(tmp_path)

    with pytest.raises(ValueError, match="carries no usable scope"):
        agent.handle_task({"id": "no-scope", "type": "chat", "text": "go"}, cancellation=FakeCancellation())

    # The refusal happens before any run state is built, so the agent is still idle after it.
    assert agent._run_state is None


def test_progress_and_typing_stop_address_through_the_run_states_scope(tmp_path):
    agent, _ = _make_agent(tmp_path)
    agent._events = RecordingAgentEvents()

    agent._progress_emitter(TEST_SCOPE, {}, "t-1")("halfway")
    agent._emit_typing_stop(TEST_SCOPE)

    progress, typing_stop = agent._events.published
    # The whole scope, and the delivery source beside it — not two of four axes.
    assert (progress.scope, progress.source) == (TEST_SCOPE, TEST_SCOPE.surface_id)
    assert (typing_stop.scope, typing_stop.source) == (TEST_SCOPE, TEST_SCOPE.surface_id)
    assert not hasattr(progress, "chat_id") and not hasattr(typing_stop, "chat_id")


def test_the_agent_span_session_no_longer_comes_from_a_conversation_key(tmp_path):
    """``session_id``/``conversation_id`` are not agent-facing, so the span carries no session."""
    import ouroboros.agent.core as core_mod

    assert not hasattr(core_mod, "_aef_task_session_id")


def test_progress_events_carry_the_tasks_transport_hints(tmp_path):
    """Delivery detail rides beside the scope: without it a dialogs group loses targeting.

    ``target_uid`` and the anchor message id are how a progress line reaches the same person
    and thread as the answer that follows it. The scope is identity and carries neither.
    """
    agent, _ = _make_agent(tmp_path)
    agent._events = RecordingAgentEvents()
    transport = {"target_uid": "alice", "source_message_id": "m-1"}

    agent._progress_emitter(TEST_SCOPE, transport, "t-1")("halfway")

    event = agent._events.published[0]
    assert event.transport == transport
    # Copied, not aliased: a later mutation of the task's dict must not reach a published one.
    transport["target_uid"] = "bob"
    assert event.transport["target_uid"] == "alice"


def test_the_progress_emitter_reads_the_transport_off_the_task(tmp_path):
    """The wiring, not just the closure: build_task_run must hand the task's own hints over."""
    agent, _ = _make_agent(tmp_path)
    agent._events = RecordingAgentEvents()

    _graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-progress", "transport": {"target_uid": "alice"}},
        scope=TEST_SCOPE,
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    _graph_ctx.events.progress("halfway")

    assert agent._events.published[0].transport == {"target_uid": "alice"}


# --- the owner's confirmation, end to end -----------------------------------


def _parked_run(tmp_path, monkeypatch, handler, *, owner_gate="true"):
    from new_core.tools.entry import ToolEntry
    from new_core.tools.schema import ToolSchema
    from tests.helpers.shared import make_catalog, make_tool_ctx, static_tool_provider
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_TOOL_CONFIRMATION_ENABLED": owner_gate}))
    schema = ToolSchema(name="data_write", description="d", parameters={"type": "object"})
    catalog = make_catalog([static_tool_provider(ToolEntry(name="data_write", schema=schema, handler=handler))])
    monkeypatch.setattr(task_graph_mod, "tool_catalog", catalog)
    store = runtime_persistence.initialize()
    agent, _ = _make_agent(tmp_path)
    agent.tool_confirmation_reader = sql_tool_confirmation_reader(store.engine)
    # Without a sink the question cannot be delivered, and the run withdraws it instead of parking.
    agent._events = RecordingAgentEvents()
    agent._emit_live_log = lambda *a, **k: None
    agent._emit_typing_stop = lambda *a, **k: None
    agent._build_tool_context = lambda task, workspace, **kwargs: make_tool_ctx(
        repo_dir=tmp_path, drive_root=tmp_path, task_workspace=workspace
    )
    agent._assemble_opening_transcript = lambda *a, **_kwargs: [{"role": "user", "content": "go"}]
    return agent, store


def test_the_owners_answer_resumes_the_run_and_the_approved_call_runs(tmp_path, monkeypatch):
    """Everything the round cannot store fails here: schemas, workspace, the decision's key."""
    import pathlib

    from ouroboros.utils import utc_now_iso
    from tests.helpers.runtime_store import claim_task

    sent: list[str] = []

    def send(ctx, **kw):
        sent.append((pathlib.Path(ctx.task_workspace) / "report.txt").read_text(encoding="utf-8"))
        return "ok"

    agent, store = _parked_run(tmp_path, monkeypatch, send)
    build_tool_context = agent._build_tool_context
    workspaces: list[pathlib.Path] = []
    contexts: list[Any] = []
    transcripts: list[str] = []

    def tool_context(task, workspace, **kwargs):
        if not workspaces:
            (pathlib.Path(workspace) / "report.txt").write_text("the report", encoding="utf-8")
        workspaces.append(workspace)
        contexts.append(build_tool_context(task, workspace, **kwargs))
        return contexts[-1]

    def opening_transcript(task, *args, **_kwargs):
        transcripts.append(task["id"])
        return [{"role": "user", "content": "send the report"}]

    agent._build_tool_context = tool_context
    agent._assemble_opening_transcript = opening_transcript
    call = ToolCall(id="c1", function=ToolCallFunction(name="data_write", arguments="{}"))
    responses = iter([LLMMessage(content="", tool_calls=[call]), LLMMessage(content="done", tool_calls=None)])
    published: dict = {}

    offered: list[list[str]] = []

    def scripted_call(_llm, _messages, _model, tools, *args, **kwargs):
        offered.append([schema.name for schema in tools or []])
        return next(responses)

    def spy_emit(env, llm, pending_events, task, text, usage, trace, start_time, drive_logs, ctx=None, **kwargs):
        published["text"] = text

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=scripted_call),
        patch("ouroboros.agent.task_pipeline.emit_task_results", side_effect=spy_emit),
    ):
        parked_task = claim_task(store, "parked").to_agent_input()
        agent.handle_task(parked_task, cancellation=FakeCancellation())

        assert store.tasks_repo.get_task("parked").status == "waiting_confirmation", "the run gave its worker back"
        assert sent == [], "nothing ran before the owner answered"
        assert (workspaces[0] / "report.txt").is_file(), "the file the question is about outlived the run"

        request = sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE)
        sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
            request.confirmation_id, decision="approved", now_iso=utc_now_iso()
        )
        sql_tool_confirmation_reader(store.engine).requeue_parked_task("parked")
        resumed_task = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a").to_agent_input()

        agent.handle_task(resumed_task, cancellation=FakeCancellation())

    assert sent == ["the report"], "the approved call ran, and found what it was approved to send"
    assert published["text"] == "done", "the run reached its answer instead of failing after the call"
    assert offered == [["data_write"], ["data_write"]], "the resumed round was handed its surface back"
    assert contexts[1].tool_surface.active_names() == {"data_write"}, "the resumed run's tools read that surface"
    assert transcripts == ["parked"], "the resumed run took its transcript from the snapshot"
    assert workspaces[1] == workspaces[0], "the resumed run picked the parked workspace up"
    assert not workspaces[1].exists(), "the finished run took its workspace with it"


def test_an_owner_who_turned_the_gate_off_runs_the_listed_call_unasked(tmp_path, monkeypatch):
    from ouroboros.storage.settings.store import load_settings
    from tests.helpers.runtime_store import claim_task

    monkeypatch.setenv("OUROBOROS_TOOL_CONFIRMATION_ENABLED", "true")
    assert load_settings().OUROBOROS_TOOL_CONFIRMATION_ENABLED is True
    written: list[str] = []
    agent, store = _parked_run(
        tmp_path, monkeypatch, lambda ctx, **kw: written.append("report") or "ok", owner_gate="false"
    )
    call = ToolCall(id="c1", function=ToolCallFunction(name="data_write", arguments="{}"))
    responses = iter([LLMMessage(content="", tool_calls=[call]), LLMMessage(content="done", tool_calls=None)])

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=lambda *a, **kw: next(responses)),
        patch("ouroboros.agent.task_pipeline.emit_task_results"),
    ):
        agent.handle_task(claim_task(store, "unasked").to_agent_input(), cancellation=FakeCancellation())

    assert written == ["report"]
    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE) is None


def test_the_resumed_run_reads_the_gate_the_owner_turned_off_while_it_waited(tmp_path, monkeypatch):
    from ouroboros.utils import utc_now_iso
    from tests.helpers.runtime_store import claim_task
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    written: list[str] = []
    agent, store = _parked_run(tmp_path, monkeypatch, lambda ctx, **kw: written.append(kw["file_path"]) or "ok")
    calls = [
        ToolCall(id="c1", function=ToolCallFunction(name="data_write", arguments='{"file_path": "a"}')),
        ToolCall(id="c2", function=ToolCallFunction(name="data_write", arguments='{"file_path": "b"}')),
    ]
    responses = iter(
        [
            LLMMessage(content="", tool_calls=[calls[0]]),
            LLMMessage(content="", tool_calls=[calls[1]]),
            LLMMessage(content="done", tool_calls=None),
        ]
    )

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=lambda *a, **kw: next(responses)),
        patch("ouroboros.agent.task_pipeline.emit_task_results"),
    ):
        agent.handle_task(claim_task(store, "parked").to_agent_input(), cancellation=FakeCancellation())
        assert written == []
        confirmations = sql_tool_confirmation_reader(store.engine)
        request = confirmations.read_open_tool_confirmation(TEST_SCOPE)
        reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_TOOL_CONFIRMATION_ENABLED": "false"}))
        confirmations.resolve_tool_confirmation(request.confirmation_id, decision="approved", now_iso=utc_now_iso())
        confirmations.requeue_parked_task("parked")
        resumed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a").to_agent_input()
        agent.handle_task(resumed, cancellation=FakeCancellation())

    assert written == ["a", "b"], "the approved call ran, and the next listed call was not asked about"
    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE) is None


@pytest.mark.parametrize("detached", [False, True], ids=["task", "durable job"])
def test_a_job_stop_ends_the_parked_run_without_the_call_and_its_workspace(tmp_path, monkeypatch, detached):
    """The stop lands on a record with no run, and the run started for it is what removes the files."""
    from ouroboros.agent_runtime.cancellation import StoreCancellation
    from ouroboros.storage.runtime_models import TaskRequest

    written: list[str] = []
    agent, store = _parked_run(tmp_path, monkeypatch, lambda ctx, **kw: written.append("report") or "ok")
    build_tool_context = agent._build_tool_context
    workspaces: list[Any] = []

    def tool_context(task, workspace, **kwargs):
        workspaces.append(workspace)
        return build_tool_context(task, workspace, **kwargs)

    agent._build_tool_context = tool_context
    call = ToolCall(id="c1", function=ToolCallFunction(name="data_write", arguments="{}"))
    model_calls: list[int] = []

    def scripted_call(*args, **kwargs):
        model_calls.append(len(model_calls))
        return LLMMessage(content="", tool_calls=[call])

    store.tasks_repo.create_task(
        TaskRequest(
            id="parked",
            type="task",
            text="work",
            detached=detached,
            chat_id=TEST_SCOPE.chat_id,
            scope=TEST_SCOPE.to_dict(),
        )
    )

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=scripted_call),
        patch("ouroboros.agent.task_pipeline.emit_task_results"),
    ):
        parked_task = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a").to_agent_input()
        agent.handle_task(parked_task, cancellation=FakeCancellation())
        assert workspaces[0].is_dir(), "the parked run kept its workspace for the answer"

        assert [record.status for record in store.tasks_repo.stop_jobs(scope=TEST_SCOPE)] == ["queued"]
        stopped_task = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a").to_agent_input()
        agent.handle_task(
            stopped_task,
            cancellation=StoreCancellation(
                store.tasks_repo,
                task_id="parked",
                scope=TEST_SCOPE,
                poll_interval_sec=0,
                claim_token=stopped_task["execution_claim"] if detached else None,
            ),
        )

    assert (len(model_calls), written) == (1, []), "the run read the stop ahead of the model and the call"
    assert not workspaces[0].exists(), "the run that read the stop took the workspace with it"


def test_an_undelivered_second_question_leaves_nothing_a_reclaim_could_replay(tmp_path, monkeypatch):
    from ouroboros.utils import utc_now_iso
    from tests.helpers.runtime_store import claim_task

    written: list[str] = []
    agent, store = _parked_run(tmp_path, monkeypatch, lambda ctx, **kw: written.append(kw["path"]) or "ok")
    calls = [
        ToolCall(id="c1", function=ToolCallFunction(name="data_write", arguments='{"path": "a"}')),
        ToolCall(id="c2", function=ToolCallFunction(name="data_write", arguments='{"path": "b"}')),
    ]
    responses = iter([LLMMessage(content="", tool_calls=calls), LLMMessage(content="done", tool_calls=None)])

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=lambda *a, **k: next(responses)),
        patch("ouroboros.agent.task_pipeline.emit_task_results"),
    ):
        agent.handle_task(claim_task(store, "parked").to_agent_input(), cancellation=FakeCancellation())
        first = sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE)
        sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
            first.confirmation_id, decision="approved", now_iso=utc_now_iso()
        )
        sql_tool_confirmation_reader(store.engine).requeue_parked_task("parked")
        agent._events = None
        resumed_task = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a").to_agent_input()
        agent.handle_task(resumed_task, cancellation=FakeCancellation())

    assert written == ["a"], "the approved call ran, and the one nobody was shown did not"
    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("parked") is None, (
        "a reclaim would rewind into the round the run left"
    )
    assert (
        sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("parked", round_idx=first.round_idx)
        == {}
    ), "a reclaim reaching that round would run the approved call again"


def test_a_workspace_left_by_another_instance_is_not_reused(tmp_path, caplog):
    """The answer landed on an instance that never held the parked run's files."""
    import logging

    from ouroboros.storage.runtime_models import PARKED_WORKSPACE_KEY

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    parked = {PARKED_WORKSPACE_KEY: str(tmp_root / "ouroboros-task-elsewhere")}

    with caplog.at_level(logging.WARNING):
        assert core._parked_workspace(parked, tmp_root) is None

    assert "not on this instance" in caplog.text
