"""Временный атом с кодом внутри обычного графа (гибридный планировщик, часть 2).

Всё настоящее, кроме двух границ: модельные атомы `locate`/`read.value` подменены теми же
дублями, что в tests/integration (`make_fake_locate_and_read_value_executors`), а код
`eph`-атомов исполняет `local_room` — ту же точку входа, что уедет в комнату станции.

План хода собирается через единственный API `plan_steps`; `GraphPlanner` достраивает
недостающие входы и обязательные гейты.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from dynamic_support import MARGIN_CODE, margin_request
from finengine.contracts import DynamicArtifact, GoalIR
from finengine.policy.sandbox_profiles import NetworkAccess, SandboxProfile, register_dynamic_profile
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom, to_atom_def
from finengine.skills.spec.graph_executor import ExecutionBudget, RunInputs
from finengine.skills.spec.graph_planner import AtomStep
from finengine.skills.spec.target import InputSpec, Target
from finengine.store.cases import SqlCaseStore, StalePlanRunError, evidence_id_of
from tests.integration.conftest import _make_fake_locate_and_read_value_executors

CELL = "ifrs_nf_ru"
OWNER, CASE, ENTITY = "owner-1", "case-1", "ent"
VALUES = {"EBITDA": 500.0, "REVENUE": 2000.0}
MARGIN_CODE_V2 = MARGIN_CODE.replace("* 100", "* 1000")        # другой код — другая версия атома


@dataclass
class CountedRead:
    """Подставной `read.value` со счётчиком: сколько раз реально «читали» отчёт."""
    inner: object
    calls: int = 0

    def __call__(self, items, ctx):
        self.calls += 1
        return self.inner(items, ctx)


@pytest.fixture
def read_value():
    locate, read = _make_fake_locate_and_read_value_executors(VALUES)
    return CountedRead(read), locate


@pytest.fixture
def base(tmp_path: Path, read_value) -> CaseRuntime:
    counted, locate = read_value
    runtime = CaseRuntime(
        store=SqlCaseStore(f"sqlite:///{tmp_path / 'ephemeral.db'}"),
        registry=build_registry(profile_id=CELL, locate_executor=locate, read_value_executor=counted),
        policy=default_policy(CELL), budget=ExecutionBudget(max_retries=0),
    )
    runtime.ensure_case(owner_id=OWNER, case_id=CASE)
    runtime.register_document(
        owner_id=OWNER, case_id=CASE, logical_name="report.pdf", document_id="doc:v1",
        document_version="sha256:v1",
    )
    return runtime


def _atom(code: str = MARGIN_CODE, **changes) -> EphemeralAtom:
    return EphemeralAtom(margin_request(**changes), code)


def _plan(runtime: CaseRuntime, name: str = "eph.ebitda_margin", output_type: str = "dyn:ebitda_margin"):
    goal = GoalIR(raw_query="маржа EBITDA", intent_kind="document_qa")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell(CELL))
    steps = (AtomStep(atom=name, output_type=output_type),)
    return runtime.plan_steps(
        owner_id=OWNER, case_id=CASE, goal_ir=goal,
        target=Target(output_type=output_type, entity_ref=ENTITY, period_ref="2024"),
        steps=steps, run_passport=passport,
    )


def _execute(runtime: CaseRuntime, planned, text: str, events: list | None = None):
    return runtime.execute(
        owner_id=OWNER, case_id=CASE, plan_run_id=planned.record.plan_run_id,
        inputs=RunInputs(document_text=text, role="other"),
        event_sink=events.append if events is not None else None,
    )


def _stored(runtime: CaseRuntime, output_type: str = "dyn:ebitda_margin") -> list[DynamicArtifact]:
    rows = runtime.store.list_artifacts(OWNER, CASE)
    return [v for row in rows if row["output_type"] == output_type for v in row["payloads"]]


def _names(planned) -> list[str]:
    return [node.node_key.atom_name for node in planned.record.graph.nodes]


# ---------------------------------------------------------------------------
# with_atoms
def test_with_atoms_adds_the_atoms_to_a_copy_and_keeps_the_original(base: CaseRuntime) -> None:
    atom = to_atom_def(_atom())

    extended = base.with_atoms([atom])

    assert extended is not base
    assert extended.registry.get("eph.ebitda_margin") is atom
    assert base.registry.get("eph.ebitda_margin") is None
    assert len(extended.registry) == len(base.registry) + 1
    assert extended.store is base.store and extended.policy is base.policy


def test_with_atoms_without_atoms_returns_the_same_runtime(base: CaseRuntime) -> None:
    assert base.with_atoms([]) is base


def test_with_atoms_rejects_a_name_already_in_the_registry(base: CaseRuntime) -> None:
    with pytest.raises(ValueError, match="дублирующееся имя"):
        base.with_atoms([to_atom_def(_atom())]).with_atoms([to_atom_def(_atom(MARGIN_CODE_V2))])


def test_with_atoms_keeps_the_execution_budget(base: CaseRuntime) -> None:
    assert base.with_atoms([to_atom_def(_atom())]).executor.budget == ExecutionBudget(max_retries=0)


# ---------------------------------------------------------------------------
# граф: путь извлечения и гейты раскрывает планировщик
def test_eph_graph_has_the_extraction_path_and_the_gates(base: CaseRuntime) -> None:
    runtime = base.with_atoms([to_atom_def(_atom())])

    planned = _plan(runtime)

    graph = planned.record.graph
    by_name: dict[str, list] = {}
    for node in graph.nodes:
        by_name.setdefault(node.node_key.atom_name, []).append(node)
    for name in ("locate", "read.value", "norm.unit", "norm.time", "verify.bounds"):
        assert len(by_name[name]) == 2, f"{name}: по узлу на EBITDA и на REVENUE"
    assert graph.root.atom_name == "eph.ebitda_margin"
    (eph,) = by_name["eph.ebitda_margin"]
    assert [key.atom_name for key in eph.requires] == ["verify.bounds", "verify.bounds"], (
        "атом читает показатели только за гейтами — сырой read.value ему недоступен"
    )
    assert eph.produces == ["dyn:ebitda_margin"] and eph.sandbox_profile == "sealed_room"
    assert graph.planner_version.startswith("dynamic")


# ---------------------------------------------------------------------------
# исполнение: DynamicArtifact ложится в Case
def test_execution_stores_a_dynamic_artifact_with_origin_and_provenance(
    base: CaseRuntime, severstal_text, counting_room,
) -> None:
    atom = _atom()
    runtime = base.with_atoms([to_atom_def(atom)])
    planned = _plan(runtime)

    result = _execute(runtime, planned, severstal_text)

    assert result.succeeded
    (artifact,) = _stored(runtime)
    assert isinstance(artifact, DynamicArtifact)
    assert artifact.payload == {"margin_pct": 25.0}
    assert artifact.origin == ["generated", "reported"]
    assert artifact.producer == atom.producer
    gated = {
        evidence_id_of(value)
        for key, values in result.evidence_by_key.items() if key.atom_name == "verify.bounds"
        for value in values
    }
    assert gated and set(artifact.input_evidence_ids) == gated, (
        "происхождение — ровно то, что атом получил от гейтов: показатели и находки проверок"
    )
    assert len(counting_room.codes) == 1 and counting_room.codes[0] == MARGIN_CODE


def test_the_code_sees_the_gated_metrics_and_never_the_document(
    base: CaseRuntime, severstal_text, local_room,
) -> None:
    echo = 'import json\n\ndef run(inputs):\n    return {"seen": json.dumps(inputs, ensure_ascii=False)}\n'
    schema = {"type": "object", "required": ["seen"], "properties": {"seen": {"type": "string"}}}
    runtime = base.with_atoms([to_atom_def(_atom(echo, output_schema=schema))])
    planned = _plan(runtime)

    assert _execute(runtime, planned, severstal_text).succeeded

    (artifact,) = _stored(runtime)
    seen = artifact.payload["seen"]
    assert "EBITDA" in seen and "REVENUE" in seen
    assert severstal_text[:80] not in seen and len(seen) < len(severstal_text)


def test_execution_streams_events_for_the_eph_node(base: CaseRuntime, severstal_text, local_room) -> None:
    runtime = base.with_atoms([to_atom_def(_atom())])
    planned = _plan(runtime)
    events: list[dict] = []

    _execute(runtime, planned, severstal_text, events)

    assert events[0]["type"] == "plan_graph"
    assert any(
        e["type"] == "node_run" and e["node_run"]["status"] == "succeeded"
        and e["node_run"]["node_key"]["atom_name"] == "eph.ebitda_margin"
        for e in events
    )


# ---------------------------------------------------------------------------
# повторный план и версии
def test_second_plan_reuses_the_result_and_runs_nothing_again(
    base: CaseRuntime, read_value, severstal_text, counting_room,
) -> None:
    counted, _locate = read_value
    first = base.with_atoms([to_atom_def(_atom())])
    assert _execute(first, _plan(first), severstal_text).succeeded
    reads, rooms = counted.calls, len(counting_room.codes)
    assert reads == 1 and rooms == 1

    second = base.with_atoms([to_atom_def(_atom())])          # новый снимок, тот же атом
    planned = _plan(second)
    result = _execute(second, planned, severstal_text)

    assert result.succeeded
    assert any(d["output_type"] == "dyn:ebitda_margin" for d in planned.record.available_leaf_keys), (
        "готовый dyn-результат найден листом Case"
    )
    assert counted.calls == reads and len(counting_room.codes) == rooms


def test_a_new_version_makes_the_old_result_incompatible_and_runs_again(
    base: CaseRuntime, severstal_text, counting_room,
) -> None:
    v1 = base.with_atoms([to_atom_def(_atom())])
    assert _execute(v1, _plan(v1), severstal_text).succeeded
    assert _leaves_of(v1, "eph.ebitda_margin") == 1

    v2 = base.with_atoms([to_atom_def(_atom(MARGIN_CODE_V2))])
    assert _leaves_of(v2, "eph.ebitda_margin") == 0, "результат старой версии не годится исправленному атому"
    result = _execute(v2, _plan(v2), severstal_text)

    assert result.succeeded and len(counting_room.codes) == 2
    assert counting_room.codes[1] == MARGIN_CODE_V2
    payloads = sorted(a.payload["margin_pct"] for a in _stored(v2))
    assert payloads == [25.0, 250.0], "обе версии лежат отдельно; новая не подменена старой"
    assert _leaves_of(v2, "eph.ebitda_margin") == 1


def _echo_margin_atom() -> EphemeralAtom:
    """Атом второго уровня: берёт результат `eph.ebitda_margin` (вход dyn:*) и возвращает его число."""
    schema = {"type": "object", "required": ["seen"],
              "properties": {"seen": {"type": "number", "x-unit": "percent"}}}
    code = "def run(inputs):\n    return {'seen': inputs['dyn:ebitda_margin'][0]['payload']['margin_pct']}\n"
    request = AtomRequest(name="eph.margin_echo", purpose="повтор маржи", output_type="dyn:margin_echo",
                          output_schema=schema, requires=(InputSpec("dyn:ebitda_margin"),))
    return EphemeralAtom(request, code)


def test_fixing_an_upstream_atom_invalidates_results_computed_from_its_old_output(
    base: CaseRuntime, severstal_text, counting_room,
) -> None:
    v1 = base.with_atoms([to_atom_def(_atom()), to_atom_def(_echo_margin_atom())])
    assert _execute(v1, _plan(v1, "eph.margin_echo", "dyn:margin_echo"), severstal_text).succeeded

    v2 = base.with_atoms([to_atom_def(_atom(MARGIN_CODE_V2)), to_atom_def(_echo_margin_atom())])
    assert _leaves_of(v2, "eph.margin_echo") == 0, "результат, посчитанный из выхода старой версии, устарел"
    assert _execute(v2, _plan(v2, "eph.margin_echo", "dyn:margin_echo"), severstal_text).succeeded

    seen = sorted(a.payload["seen"] for a in _stored(v2, "dyn:margin_echo"))
    assert seen == [25.0, 250.0], "новый результат посчитан из новой версии upstream, а не взят старый"


def _leaves_of(runtime: CaseRuntime, atom_name: str) -> int:
    passport = runtime.store.load_case(OWNER, CASE).run_passport
    index = runtime.available_index(owner_id=OWNER, case_id=CASE, run_passport=passport)
    return sum(1 for leaf in index.all_leaves() if leaf.node_key.atom_name == atom_name)


# ---------------------------------------------------------------------------
# песочница: нарушение профиля блокирует узел до запуска кода
def _net_profile() -> str:
    return register_dynamic_profile("graph_room_with_network", SandboxProfile(
        name="graph_room_with_network", network_access=NetworkAccess.ALLOWLIST,
        allowed_hosts=("example.com",), code_execution=True, rare_explicit_grant=True,
        description="тестовый профиль: код с сетью",
    ))


@pytest.mark.parametrize("profile", ["compute_no_network", "graph_room_with_network"])
def test_generated_atom_under_a_wrong_profile_is_blocked_and_the_code_never_runs(
    base: CaseRuntime, severstal_text, counting_room, profile,
) -> None:
    if profile == "graph_room_with_network":
        _net_profile()
    definition = replace(to_atom_def(_atom()), sandbox_profile=profile)
    runtime = base.with_atoms([definition])
    planned = _plan(runtime)

    result = _execute(runtime, planned, severstal_text)

    root = result.node_runs[planned.record.graph.root]
    assert root.status == "blocked" and "сгенерированный код" in root.error
    assert not result.succeeded and counting_room.codes == []
    assert _stored(runtime) == []


# ---------------------------------------------------------------------------
# сбои атома — статус узла, а не падение прогона
def test_code_error_fails_only_the_eph_node(base: CaseRuntime, severstal_text, local_room) -> None:
    runtime = base.with_atoms([to_atom_def(_atom("def run(inputs):\n    return {'margin_pct': 1 / 0}\n"))])
    planned = _plan(runtime)

    result = _execute(runtime, planned, severstal_text)

    root = result.node_runs[planned.record.graph.root]
    assert root.status == "failed" and "ZeroDivisionError" in root.error
    gates = [run for key, run in result.node_runs.items() if key.atom_name == "verify.bounds"]
    assert gates and all(run.status == "succeeded" for run in gates)
    assert _stored(runtime) == []


def test_schema_violation_fails_the_node_and_stores_nothing(
    base: CaseRuntime, severstal_text, local_room,
) -> None:
    runtime = base.with_atoms([to_atom_def(_atom("def run(inputs):\n    return {'margin_pct': 'много'}\n"))])
    planned = _plan(runtime)

    result = _execute(runtime, planned, severstal_text)

    root = result.node_runs[planned.record.graph.root]
    assert root.status == "failed" and "output_schema" in root.error
    assert _stored(runtime) == []


# ---------------------------------------------------------------------------
# сверка registry_fingerprint
def test_plan_and_execution_through_one_snapshot_pass_the_fingerprint_check(
    base: CaseRuntime, severstal_text, local_room,
) -> None:
    runtime = base.with_atoms([to_atom_def(_atom())])
    planned = _plan(runtime)

    assert planned.record.registry_fingerprint == runtime.registry.fingerprint()
    assert _execute(runtime, planned, severstal_text).succeeded


def test_execution_through_a_registry_without_the_atom_is_stale_not_a_silent_empty_success(
    base: CaseRuntime, severstal_text, local_room,
) -> None:
    planned = _plan(base.with_atoms([to_atom_def(_atom())]))

    with pytest.raises(StalePlanRunError, match="registry fingerprint"):
        _execute(base, planned, severstal_text)

    record = base.store.load_plan_run(OWNER, CASE, planned.record.plan_run_id)
    assert record.status == "stale"


def test_a_snapshot_with_another_version_of_the_atom_is_stale(
    base: CaseRuntime, severstal_text, local_room,
) -> None:
    planned = _plan(base.with_atoms([to_atom_def(_atom())]))
    other = base.with_atoms([to_atom_def(_atom(MARGIN_CODE_V2))])

    with pytest.raises(StalePlanRunError, match="registry fingerprint"):
        _execute(other, planned, severstal_text)

