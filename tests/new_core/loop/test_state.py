"""``RunConfig`` / ``RunState``: the required owner scope, and the parked run's snapshot."""

from __future__ import annotations

import dataclasses
import json

import pytest

from new_core.identity import OwnerScope
from new_core.llm.types import LLMMessage, ToolCall, ToolCallFunction
from new_core.loop.context import RunConfig
from new_core.loop.state import RunState, make_run_state
from tests.new_core.loop._fakes import make_config


def test_make_run_state_seeds_scope_from_config() -> None:
    scope = OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="42")
    config = make_config(scope=scope)

    state = make_run_state(config, messages=[], tool_schemas=[], active_tool_names=set())

    assert state.scope is scope


def test_a_run_config_without_a_scope_cannot_be_built() -> None:
    complete = dataclasses.asdict(make_config())
    complete.pop("scope")

    with pytest.raises(TypeError):
        RunConfig(**complete)


def test_a_run_state_without_a_scope_cannot_be_built() -> None:
    with pytest.raises(TypeError):
        RunState(  # type: ignore[call-arg]
            task_id="t",
            messages=[],
            tool_schemas=[],
            active_tool_names=set(),
            active_model="m",
            active_effort="medium",
            active_use_local=False,
        )


def test_the_scope_field_is_not_optional() -> None:
    annotations = {f.name: f.type for f in dataclasses.fields(RunState)}
    assert "None" not in str(annotations["scope"])


def test_run_state_carries_no_soft_cap_info_field() -> None:
    """``cap_info`` was write-only: produced by the soft cap, read by nothing."""
    assert "cap_info" not in {f.name for f in dataclasses.fields(RunState)}


def _mid_round_state() -> RunState:
    state = make_run_state(
        make_config(),
        messages=[{"role": "user", "content": "write the file"}],
        tool_schemas=[{"name": "file_write"}],
        active_tool_names={"file_write"},
    )
    state.round_idx = 3
    state.active_model = "m2"
    state.active_effort = "high"
    state.active_use_local = True
    state.accumulated_usage["total_tokens"] = 120
    state.llm_trace["reasoning_notes"].append("about to write")
    state.last_resp = LLMMessage(
        tool_calls=[ToolCall(id="call_1", function=ToolCallFunction(name="file_write", arguments='{"path": "a.txt"}'))]
    )
    state.pending_compaction = 20
    state.checkpoint_injected = True
    state.owner_msg_seen.add("msg-1")
    return state


_TRAVELS = (
    "messages",
    "active_tool_names",
    "active_model",
    "active_effort",
    "active_use_local",
    "accumulated_usage",
    "llm_trace",
    "round_idx",
    "last_resp",
    "pending_compaction",
    "checkpoint_injected",
    "owner_msg_seen",
)


def test_a_snapshot_survives_the_json_column_it_is_stored_in() -> None:
    parked = _mid_round_state()
    resumed = make_run_state(make_config(), messages=[], tool_schemas=[], active_tool_names=set())

    resumed.restore(json.loads(json.dumps(parked.snapshot())))

    assert {f: getattr(resumed, f) for f in _TRAVELS} == {f: getattr(parked, f) for f in _TRAVELS}


def test_a_snapshot_written_by_another_version_restores_what_it_carries() -> None:
    """Two versions run side by side through a rollout, so a snapshot may predate a field."""
    parked = _mid_round_state().snapshot()
    parked.pop("owner_msg_seen")

    resumed = make_run_state(make_config(), messages=[], tool_schemas=[], active_tool_names=set())
    resumed.restore(parked)

    assert resumed.round_idx == 3, "the fields the snapshot does carry are restored"
    assert resumed.owner_msg_seen == set()


def test_restore_writes_into_the_objects_the_run_already_handed_out() -> None:
    # Other holders keep these very objects; replacing them leaves those holders pre-park.
    resumed = make_run_state(make_config(), messages=[], tool_schemas=[], active_tool_names=set())
    messages = resumed.messages
    schemas = resumed.tool_schemas
    names = resumed.active_tool_names
    usage = resumed.accumulated_usage
    trace = resumed.llm_trace
    seen = resumed.owner_msg_seen

    resumed.restore(_mid_round_state().snapshot())

    assert resumed.messages is messages
    assert resumed.tool_schemas is schemas
    assert resumed.active_tool_names is names
    assert resumed.accumulated_usage is usage
    assert resumed.llm_trace is trace
    assert resumed.owner_msg_seen is seen
    assert messages == [{"role": "user", "content": "write the file"}]
    assert names == {"file_write"}
    assert usage == {"total_tokens": 120}
