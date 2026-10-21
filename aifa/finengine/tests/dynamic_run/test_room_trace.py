"""Трасса хода идёт от контекста узла временного атома в комнату станции (ADR-0007, CONTRACTS.md §5)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from finengine.skills.spec import sealed_room
from finengine.skills.spec.atom_intake import Registered, check_code
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom, to_atom_def
from finengine.skills.spec.graph_executor import NodeArgs, RunInputs


def _atom() -> EphemeralAtom:
    request = AtomRequest(
        name="eph.echo", purpose="эхо", output_type="dyn:echo", output_schema={"type": "object"}, requires=(),
    )
    return EphemeralAtom(request, "def run(inputs):\n    return {}\n")


def _ran_with(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str]) -> list[dict]:
    calls: list[dict] = []

    def room(code: str, inputs: dict, **kwargs: object) -> sealed_room.CodeRun:
        calls.append(kwargs)
        return sealed_room.CodeRun({}, None)

    monkeypatch.setattr(sealed_room, "run_code", room)
    ctx = SimpleNamespace(run_id="run-1", inputs=RunInputs(extra=extra), dependencies=lambda: ())
    to_atom_def(_atom()).executor(NodeArgs(), ctx)
    return calls


def test_the_node_gives_the_trace_of_its_turn_to_the_room(monkeypatch: pytest.MonkeyPatch) -> None:
    (call,) = _ran_with(monkeypatch, {"trace_id": "trace-of-the-turn", "owner_id": "7"})

    assert call["trace_id"] == "trace-of-the-turn"


def test_a_run_without_a_turn_gives_the_room_no_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    (call,) = _ran_with(monkeypatch, {"owner_id": "7"})

    assert call["trace_id"] == ""


def test_the_check_of_a_registered_code_runs_in_the_room_with_the_trace_of_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0007: проверка кода подзадачи — тоже вызов станции этого хода; её видно в журнале станции по трассе."""
    calls: list[dict] = []

    def room(code: str, inputs: dict, **kwargs: object) -> sealed_room.CodeRun:
        calls.append(kwargs)
        return sealed_room.CodeRun({}, None)

    monkeypatch.setattr(sealed_room, "run_code", room)
    request = AtomRequest(
        name="eph.echo", purpose="эхо", output_type="dyn:echo", output_schema={"type": "object"}, requires=(),
    )

    verdict = check_code(request, "def run(inputs):" + chr(10) + "    return {}", {}, trace_id="trace-of-the-turn")

    assert verdict.error is None
    assert calls[0]["trace_id"] == "trace-of-the-turn"


def test_the_cycle_gives_the_registration_the_trace_of_its_turn(
    base, severstal_text, local_room, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Цикл несёт трассу хода из входов прогона в проверку кода, который сдаёт подзадача."""
    from dynamic_support import MARGIN_CODE, margin_request
    from finengine.contracts import GoalIR
    from finengine.skills.spec.dynamic_run import DynamicRun
    from finengine.skills.spec.target import Target
    from test_atom_inputs import _run_echo
    from test_ephemeral_in_graph import CASE, OWNER
    from tests.dynamic_run.conftest import local_run_code

    runtime, _ = _run_echo(base, severstal_text)
    calls: list[dict] = []

    def room(code: str, inputs: dict, **kwargs: object) -> sealed_room.CodeRun:
        calls.append(kwargs)
        return local_run_code(code, inputs, **kwargs)

    monkeypatch.setattr(sealed_room, "run_code", room)
    cycle = DynamicRun(
        runtime=runtime, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="вопрос", intent_kind="document_qa"),
        target=Target(output_type="QuoteEvidence", entity_ref="e"),
        inputs=RunInputs(extra={"trace_id": "trace-of-the-turn"}),
    )

    outcome = cycle.register_atom(margin_request(), MARGIN_CODE)

    assert isinstance(outcome, Registered)
    assert calls and {call["trace_id"] for call in calls} == {"trace-of-the-turn"}
