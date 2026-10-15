"""Повторный запрос использует доказательства того же Case (часть 11)."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from finengine.contracts import GoalIR, ScalarFact
from finengine.skills import production_runtime as production
from finengine.skills.atoms import intent
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.dynamic_run import DynamicRun, RunLimits
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.graph_planner import AtomStep
from finengine.skills.spec.target import Target
from finengine.store.cases import SqlCaseStore
from tests.integration.conftest import _make_fake_locate_and_read_value_executors


OWNER = "owner-11"
CASE = "case-11"
DOCUMENT = "report-11.pdf"
CONCEPT = "SALES_REVENUE"
LIVE_INTENT_FIXTURE = Path(__file__).with_name("fixtures") / "live_intent_two_periods.json"


@dataclass
class CountedRead:
    calls: int = 0
    concepts: tuple[str, ...] = (CONCEPT,)
    entity_ref: str | None = None

    def __call__(self, items, ctx):  # noqa: ARG002
        self.calls += 1
        result = {}
        for args in items:
            if self.entity_ref is None:
                self.entity_ref = args.entity_ref
            if args.concept not in self.concepts or args.entity_ref != self.entity_ref or args.period_ref:
                result[args.concept] = ()
                continue
            result[args.concept] = tuple(
                ScalarFact(
                    produced_by_atom="read.value",
                    produced_by_atom_version="1.0.0",
                    source_ref=f"fake:{period}",
                    concept=args.concept,
                    period_ref=period,
                    value=value,
                    unit="RUB_MLN",
                )
                for period, value in (("6M 2026", 1453957.053), ("6M 2025", 1398538.772))
            )
        return result


@dataclass
class ReportPeriodsRead:
    """Сырой `read_value_batch` не знает запрошенный период: отдаёт все периоды отчёта,
    а фильтр по запросу делает обёртка `_wrap_extracted_metric` (в отличие от `CountedRead`)."""

    calls: int = 0

    def __call__(self, items, ctx):  # noqa: ARG002
        self.calls += 1
        return {
            args.concept: tuple(
                ScalarFact(
                    produced_by_atom="read.value",
                    produced_by_atom_version="1.0.0",
                    source_ref=f"fake:{period}",
                    concept=args.concept,
                    period_ref=period,
                    value=value,
                    unit="RUB_MLN",
                )
                for period, value in (("6M 2026", 1453957.053), ("6M 2025", 1398538.772))
            )
            for args in items
        }


def _goal(
    period_ref: str | None,
    *,
    concept: str = CONCEPT,
    entity_ref: str | None = None,
) -> GoalIR:
    return GoalIR(
        raw_query="покажи выручку",
        intent_kind="extract_registered_metric",
        targets=[{"concept": concept, "output_kind": "scalar"}],
        entity_ref=entity_ref,
        period_ref=period_ref,
    )


def _metric_ids(manifest: dict) -> list[str]:
    values = manifest.get("results") or [manifest["result"]]
    return [value["scalar_fact"]["evidence_id"] for value in values]


def _artifact_count(store: SqlCaseStore, output_type: str) -> int:
    return sum(
        row["output_type"] == output_type
        for row in store.list_artifacts(OWNER, CASE, valid_only=False)
    )


def _periods(manifest: dict) -> list[str]:
    values = manifest.get("results") or [manifest["result"]]
    return [value["period_ref"] for value in values]


def _stored_metric_ids(store: SqlCaseStore) -> set[str]:
    return {
        value.scalar_fact.evidence_id
        for row in store.list_artifacts(OWNER, CASE)
        if row["output_type"] == "ExtractedMetric"
        for value in row["payloads"]
    }


@dataclass
class PlannerReplies:
    replies: list[dict]
    prompts: list[str] = field(default_factory=list)

    async def __call__(self, system: str, prompt: str) -> str:  # noqa: ARG002
        self.prompts.append(prompt)
        return json.dumps(self.replies.pop(0), ensure_ascii=False)


@dataclass
class IntentRecorder:
    pairs: list[dict] = field(default_factory=list)

    async def __call__(
        self, system: str, prompt: str, *, model_version: str | None = None,
    ) -> str:
        reply = await intent._default_complete(system, prompt, model_version=model_version)
        self.pairs.append({"prompt": prompt, "reply": reply})
        return reply


@dataclass
class IntentReplies:
    replies: list[str]

    async def __call__(
        self, system: str, prompt: str, *, model_version: str | None = None,  # noqa: ARG002
    ) -> str:
        return self.replies.pop(0)


def _read_move(period_ref: str) -> dict:
    return {
        "objective": "вернуть сохранённую выручку",
        "reason": "значения уже извлечены в Case",
        "action": "execute",
        "scenarios": [],
        "new_atoms": [],
        "steps": [{"atom": "read.value", "concept": CONCEPT, "period_ref": period_ref}],
        "answer": "",
        "evidence_ids": [],
    }


def _give_up() -> dict:
    return {
        "objective": "остановиться",
        "reason": "новых результатов нет",
        "action": "give_up",
        "scenarios": [],
        "new_atoms": [],
        "steps": [],
        "answer": "Использованы уже сохранённые значения.",
        "evidence_ids": [],
    }


def test_all_periods_then_two_explicit_periods_reuses_the_same_metrics(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'reuse.db'}")
    counted_read = CountedRead()
    locate, _unused = _make_fake_locate_and_read_value_executors({CONCEPT: 1.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru",
        locate_executor=locate,
        read_value_executor=counted_read,
    )
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)

    first = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal(None),
        store=store,
    )
    assert first is not None and "error" not in first.manifest
    first_ids = _metric_ids(first.manifest)
    assert len(first_ids) == 2 and counted_read.calls == 1
    infrastructure_before = {
        name: _artifact_count(store, name) for name in ("DocumentText", "RunPassport")
    }

    second = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal("6M 2026, 6M 2025"),
        store=store,
    )

    assert second is not None and "error" not in second.manifest, second.manifest
    assert _metric_ids(second.manifest) == first_ids
    assert counted_read.calls == 1, "повторный запрос не должен снова исполнять read.value"
    assert {
        name: _artifact_count(store, name) for name in ("DocumentText", "RunPassport")
    } == infrastructure_before


def test_plan_steps_reuses_a_successful_read_value_from_the_same_case(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'steps.db'}")
    counted_read = CountedRead()
    locate, _unused = _make_fake_locate_and_read_value_executors({CONCEPT: 1.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru",
        locate_executor=locate,
        read_value_executor=counted_read,
    )
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)
    first_goal = _goal(None)
    first = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=first_goal,
        store=store,
    )
    assert first is not None and "error" not in first.manifest
    first_ids = _metric_ids(first.manifest)
    row = next(
        item for item in store.list_artifacts(OWNER, CASE)
        if item["output_type"] == "ExtractedMetric" and item["concept"] == CONCEPT
    )
    runtime = CaseRuntime(
        store=store,
        registry=registry,
        policy=default_policy("ifrs_nf_ru"),
    )
    second_goal = _goal("6M 2026, 6M 2025")
    passport = store.load_case(OWNER, CASE).run_passport
    available = runtime.available_index(owner_id=OWNER, case_id=CASE, run_passport=passport)
    reusable = available.lookup(
        "ExtractedMetric",
        CONCEPT,
        entity_ref=row["entity_ref"],
        period_ref=second_goal.period_ref,
        basis=None,
        document_version=row["document_version"],
        policy_version=default_policy("ifrs_nf_ru").policy_version,
    )
    assert reusable is not None

    planned = runtime.plan_steps(
        owner_id=OWNER,
        case_id=CASE,
        goal_ir=second_goal,
        target=Target(
            output_type="ExtractedMetric",
            concept_id=CONCEPT,
            entity_ref=row["entity_ref"],
            period_ref=second_goal.period_ref,
            document_version=row["document_version"],
        ),
        steps=(AtomStep(
            atom="read.value",
            output_type="ExtractedMetric",
            concept=CONCEPT,
            period_ref=second_goal.period_ref,
        ),),
        run_passport=passport,
    )
    result = runtime.execute(
        owner_id=OWNER,
        case_id=CASE,
        plan_run_id=planned.record.plan_run_id,
    )

    assert result.succeeded
    assert {key.atom_name for key in result.node_runs} == {"verify.bounds"}
    assert [
        fact.scalar_fact.evidence_id
        for values in result.evidence_by_key.values()
        for fact in values
        if getattr(fact, "output_type", None) == "ExtractedMetric"
    ] == first_ids
    assert counted_read.calls == 1


@pytest.mark.parametrize(
    ("concept", "requested", "expected"),
    [
        ("SALES_REVENUE", "1H 2026", ["6M 2026"]),
        ("NET_INCOME", "6M 2026 and 1H 2025", ["6M 2026", "6M 2025"]),
        ("SALES_REVENUE", None, ["6M 2026", "6M 2025"]),
    ],
)
def test_written_all_period_leaf_is_found_for_logically_equivalent_requests(
    tmp_path: Path,
    severstal_text: str,
    monkeypatch,
    concept: str,
    requested: str | None,
    expected: list[str],
) -> None:
    store = SqlCaseStore(f"sqlite:///{tmp_path / f'{concept}.db'}")
    counted_read = CountedRead(concepts=(concept,))
    locate, _unused = _make_fake_locate_and_read_value_executors({concept: 1.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru",
        locate_executor=locate,
        read_value_executor=counted_read,
    )
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)
    first = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal(None, concept=concept),
        store=store,
    )
    assert first is not None and "error" not in first.manifest

    second = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal(requested, concept=concept),
        store=store,
    )

    assert second is not None and "error" not in second.manifest
    assert _periods(second.manifest) == expected
    assert counted_read.calls == 1


def test_period_matrix_reuses_case_and_reports_the_periods_on_a_mismatch(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'matrix.db'}")
    counted_read = CountedRead()
    locate, _unused = _make_fake_locate_and_read_value_executors({CONCEPT: 1.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru",
        locate_executor=locate,
        read_value_executor=counted_read,
    )
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)

    outcomes = []
    atoms_by_request = []
    for requested in (None, "6M 2026, 6M 2025", "1H 2026", None, "FY 2030"):
        events: list[dict] = []
        outcomes.append(production.execute_routed_goal(
            owner_id=OWNER,
            case_id=CASE,
            document_id=DOCUMENT,
            document_text=severstal_text,
            goal=_goal(requested),
            store=store,
            event_sink=events.append,
        ))
        graph = next(event for event in events if event.get("type") == "plan_graph")
        atoms_by_request.append([node["node_key"]["atom_name"] for node in graph["nodes"]])

    assert [_periods(outcome.manifest) for outcome in outcomes[:4]] == [
        ["6M 2026", "6M 2025"],
        ["6M 2026", "6M 2025"],
        ["6M 2026"],
        ["6M 2026", "6M 2025"],
    ]
    mismatch = outcomes[-1].manifest["error"]
    assert mismatch["code"] == "canonical_period_mismatch"
    assert "6M 2025" in mismatch["message"] and "6M 2026" in mismatch["message"]
    assert counted_read.calls == 1
    assert set(atoms_by_request[0]) >= {"identify.classify", "locate", "read.value"}
    assert all(atoms == ["verify.bounds"] for atoms in atoms_by_request[1:])


@pytest.mark.parametrize("requested", ["6M 2026, 6M 2025", "6M 2026 и 1H 2025"])
def test_two_explicit_periods_in_a_fresh_case_return_both_values(
    tmp_path: Path, severstal_text: str, monkeypatch, requested: str,
) -> None:
    """Нет листа «все периоды» для переиспользования: список периодов читает сам `read.value`."""
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'fresh.db'}")
    read = ReportPeriodsRead()
    locate, _unused = _make_fake_locate_and_read_value_executors({CONCEPT: 1.0})
    registry = build_registry(profile_id="ifrs_nf_ru", locate_executor=locate, read_value_executor=read)
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)

    outcome = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal(requested),
        store=store,
    )

    assert outcome is not None and "error" not in outcome.manifest, outcome.manifest
    assert _periods(outcome.manifest) == ["6M 2026", "6M 2025"]
    assert read.calls == 1


def _first_all_period_run(tmp_path: Path, text: str, monkeypatch) -> tuple[SqlCaseStore, ReportPeriodsRead, dict]:
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'stale.db'}")
    counted_read = ReportPeriodsRead()
    locate, _unused = _make_fake_locate_and_read_value_executors({CONCEPT: 1.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate, read_value_executor=counted_read,
    )
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)
    first = production.execute_routed_goal(
        owner_id=OWNER, case_id=CASE, document_id=DOCUMENT, document_text=text,
        goal=_goal(None), store=store,
    )
    assert first is not None and "error" not in first.manifest and counted_read.calls == 1
    return store, counted_read, first.manifest


def test_all_period_leaf_of_a_replaced_document_is_not_reused(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store, counted_read, first = _first_all_period_run(tmp_path, severstal_text, monkeypatch)

    second = production.execute_routed_goal(
        owner_id=OWNER, case_id=CASE, document_id=DOCUMENT,
        document_text=severstal_text + "\n\nИзмененная версия отчёта.",
        goal=_goal("6M 2026"), store=store,
    )

    assert second is not None and "error" not in second.manifest, second.manifest
    assert counted_read.calls == 2, "значение прошлой версии документа не должно быть переиспользовано"
    assert _metric_ids(second.manifest) != _metric_ids(first)[:1]


def test_all_period_leaf_of_another_methodology_is_not_reused(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    monkeypatch.setenv("FINENGINE_METHODOLOGY_VERSION", "m-1")
    store, counted_read, _first = _first_all_period_run(tmp_path, severstal_text, monkeypatch)
    monkeypatch.setenv("FINENGINE_METHODOLOGY_VERSION", "m-2")

    second = production.execute_routed_goal(
        owner_id=OWNER, case_id=CASE, document_id=DOCUMENT, document_text=severstal_text,
        goal=_goal("6M 2026"), store=store,
    )

    assert second is not None and "error" not in second.manifest, second.manifest
    assert counted_read.calls == 2, "паспорт с другой методикой не должен получать старый лист"


def _case_leaf(store: SqlCaseStore, row: dict, policy_version: str):
    runtime = CaseRuntime(
        store=store, registry=build_registry(profile_id="ifrs_nf_ru"),
        policy=default_policy("ifrs_nf_ru", policy_version=policy_version),
    )
    passport = store.load_case(OWNER, CASE).run_passport
    return runtime.available_index(owner_id=OWNER, case_id=CASE, run_passport=passport).lookup(
        "ExtractedMetric", CONCEPT, entity_ref=row["entity_ref"], period_ref="6M 2026",
        document_version=row["document_version"], policy_version=policy_version,
    )


def test_all_period_leaf_of_another_policy_version_is_not_found(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store, _counted_read, _first = _first_all_period_run(tmp_path, severstal_text, monkeypatch)
    row = next(
        item for item in store.list_artifacts(OWNER, CASE)
        if item["output_type"] == "ExtractedMetric" and item["concept"] == CONCEPT
    )

    assert _case_leaf(store, row, row["policy_version"]) is not None
    assert _case_leaf(store, row, "99.0.0") is None


def _step_plan_atoms(runtime: CaseRuntime, step: AtomStep, available, passport, row: dict) -> set[str]:
    build, _args = runtime.planner.plan_with_atoms(
        [step], 0, available=available, run_passport=passport, entity_ref=row["entity_ref"],
        document_version=row["document_version"],
    )
    return {node.node_key.atom_name for node in build.graph.nodes}


def test_explicit_step_is_not_replaced_by_a_leaf_of_another_producer(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store, _read, _first = _first_all_period_run(tmp_path, severstal_text, monkeypatch)
    row = next(
        item for item in store.list_artifacts(OWNER, CASE)
        if item["output_type"] == "ExtractedMetric" and item["concept"] == CONCEPT
    )
    registry = build_registry(profile_id="ifrs_nf_ru")
    runtime = CaseRuntime(store=store, registry=registry, policy=default_policy("ifrs_nf_ru"))
    passport = store.load_case(OWNER, CASE).run_passport
    available = runtime.available_index(owner_id=OWNER, case_id=CASE, run_passport=passport)
    step = AtomStep(atom="read.value", output_type="ExtractedMetric", concept=CONCEPT, period_ref="6M 2026")

    assert "read.value" not in _step_plan_atoms(runtime, step, available, passport, row)

    key, leaf = next(
        (key, leaf) for key, leaf in available.entries()
        if key[0] == "ExtractedMetric" and key[1] == CONCEPT
    )
    foreign = leaf.__class__(
        node_key=leaf.node_key,
        evidence_ids=leaf.evidence_ids,
        evidence=tuple(
            metric.model_copy(update={
                "scalar_fact": metric.scalar_fact.model_copy(update={"produced_by_atom": "calc.formula"}),
            })
            for metric in leaf.evidence
        ),
    )
    available.put(
        "ExtractedMetric", foreign, concept=key[1], entity_ref=key[2], period_ref=key[3],
        basis=key[4], document_version=key[5], policy_version=key[6],
    )

    assert "read.value" in _step_plan_atoms(runtime, step, available, passport, row), (
        "чужой производитель не заменяет явно выбранный шаг"
    )


def test_a_requested_period_missing_from_the_report_is_named_to_the_user(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store, _read, _first = _first_all_period_run(tmp_path, severstal_text, monkeypatch)

    answered = production.execute_routed_goal(
        owner_id=OWNER, case_id=CASE, document_id=DOCUMENT, document_text=severstal_text,
        goal=_goal("6M 2026, 6M 2025"), store=store,
    )
    partial = production.execute_routed_goal(
        owner_id=OWNER, case_id=CASE, document_id=DOCUMENT, document_text=severstal_text,
        goal=_goal("6M 2026, FY 2030"), store=store,
    )

    assert answered.manifest["not_performed"] == []
    assert _periods(partial.manifest) == ["6M 2026"]
    assert [line.split(" (")[0] for line in partial.manifest["not_performed"]] == ["нет данных за период FY 2030"]


def test_another_entity_in_the_same_case_does_not_receive_the_issuers_metric(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'entity.db'}")
    counted_read = CountedRead()
    locate, _unused = _make_fake_locate_and_read_value_executors({CONCEPT: 1.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru",
        locate_executor=locate,
        read_value_executor=counted_read,
    )
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)
    first = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal(None),
        store=store,
    )
    assert first is not None and "error" not in first.manifest

    other = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal(None, entity_ref="ДРУГАЯ КОМПАНИЯ"),
        store=store,
    )

    assert other is not None and other.manifest["error"]["code"] == "metric_not_found"
    assert counted_read.calls == 2, "значение эмитента не должно стать листом другой сущности"


def test_reused_dynamic_move_keeps_ids_and_counts_as_idle(
    tmp_path: Path, severstal_text: str, monkeypatch,
) -> None:
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'dynamic.db'}")
    counted_read = CountedRead()
    locate, _unused = _make_fake_locate_and_read_value_executors({CONCEPT: 1.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru",
        locate_executor=locate,
        read_value_executor=counted_read,
    )
    monkeypatch.setattr(production, "build_registry", lambda **kwargs: registry)
    first = production.execute_routed_goal(
        owner_id=OWNER,
        case_id=CASE,
        document_id=DOCUMENT,
        document_text=severstal_text,
        goal=_goal(None),
        store=store,
    )
    assert first is not None and "error" not in first.manifest
    ids_before = _metric_ids(first.manifest)
    stored_ids_before = _stored_metric_ids(store)
    assert set(ids_before) <= stored_ids_before
    rows_before = {
        name: _artifact_count(store, name) for name in ("DocumentText", "RunPassport", "ExtractedMetric")
    }
    row = next(
        item for item in store.list_artifacts(OWNER, CASE)
        if item["output_type"] == "ExtractedMetric" and item["concept"] == CONCEPT
    )
    runtime = CaseRuntime(store=store, registry=registry, policy=default_policy("ifrs_nf_ru"))
    period_ref = "6M 2026, 6M 2025"
    results = []
    planners = []
    for _turn in range(2):
        planner = PlannerReplies([_read_move(period_ref), _give_up()])
        planners.append(planner)
        dynamic = DynamicRun(
            runtime=runtime,
            owner_id=OWNER,
            case_id=CASE,
            goal=_goal(period_ref),
            target=Target(
                output_type="ExtractedMetric",
                concept_id=CONCEPT,
                entity_ref=row["entity_ref"],
                period_ref=period_ref,
                document_version=row["document_version"],
            ),
            inputs=RunInputs(document_text=severstal_text),
            complete_fn=planner,
            limits=RunLimits(max_moves=3, idle_moves=1),
            run_passport=store.load_case(OWNER, CASE).run_passport,
        )
        results.append(dynamic.run(deterministic_error="проверка повторного чтения"))

    assert all(result.status == "gave_up" for result in results)
    assert all(result.evidence == () for result in results), (
        "сводка хода по Р4 включает только evidence, созданные в этом DynamicRun, не прошлые сообщения Case"
    )
    assert all(
        len(planner.prompts) == 2 and "без новых результатов" in planner.prompts[-1]
        for planner in planners
    )
    assert all(result.journal[0].outcome.startswith("новых результатов нет") for result in results)
    assert counted_read.calls == 1
    assert {
        name: _artifact_count(store, name) for name in ("DocumentText", "RunPassport", "ExtractedMetric")
    } == rows_before
    assert _stored_metric_ids(store) == stored_ids_before


@pytest.mark.skipif(
    os.environ.get("AIFA_LIVE_MODEL") != "1",
    reason="нужен AIFA_LIVE_MODEL=1 (и ключ модели)",
)
def test_live_intent_keeps_both_user_named_periods_and_records_the_reply() -> None:
    recorder = IntentRecorder()

    decision = asyncio.run(intent.parse_message(
        "Покажи только уже извлечённую выручку SALES_REVENUE за 6M 2026 и 6M 2025.",
        complete_fn=recorder,
        allow_answer=False,
    ))
    LIVE_INTENT_FIXTURE.parent.mkdir(exist_ok=True)
    LIVE_INTENT_FIXTURE.write_text(
        json.dumps(recorder.pairs, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert decision.goal is not None
    assert [target.concept for target in decision.goal.targets] == [CONCEPT]
    assert decision.goal.period_ref is not None
    assert "2026" in decision.goal.period_ref and "2025" in decision.goal.period_ref


@pytest.mark.skipif(
    not LIVE_INTENT_FIXTURE.exists(),
    reason="фикстура живого intent ещё не записана",
)
def test_recorded_live_two_period_intent_is_accepted_without_network() -> None:
    pairs = json.loads(LIVE_INTENT_FIXTURE.read_text(encoding="utf-8"))
    replies = IntentReplies([pair["reply"] for pair in pairs])

    decision = asyncio.run(intent.parse_message(
        "Покажи только уже извлечённую выручку SALES_REVENUE за 6M 2026 и 6M 2025.",
        complete_fn=replies,
        allow_answer=False,
    ))

    assert decision.goal is not None and decision.goal.period_ref is not None
    assert "2026" in decision.goal.period_ref and "2025" in decision.goal.period_ref
