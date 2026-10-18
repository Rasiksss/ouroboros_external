"""Трасса хода идёт от контекста узла временного атома в комнату станции (ADR-0007, CONTRACTS.md §5)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from finengine.skills.spec import sealed_room
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
