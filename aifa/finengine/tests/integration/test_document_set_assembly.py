"""Срез 8 (B9) — `set.assemble` через настоящий `PlanGraph`+`GraphExecutor`,
не изолированный код (тот же Definition of Done, что уже проверен для
`read.structure`/`read.quote`/`read.entities` в `test_graph_structure_quote_
entities.py`). Один контролируемый многодокументный приёмочный сценарий,
явно названный карточкой 18: «отчёт + пресс-релиз одного эмитента» — вторая
пара («один эмитент, два периода») и все остальные комбинации документов
сознательно НЕ покрываются здесь (карточка: «начинать только с одного
контролируемого сценария»).

Кандидаты (`DocumentCandidate`) подаются в граф ТЕМ ЖЕ существующим
механизмом, которым `tests/planner/test_available_leaf_reuse.py` и
`tests/executor/test_idempotent_resume.py` уже проверяют «уже доступный факт
не порождает второго производителя» — `AvailableIndex.put(...)` для
раскрываемости на этапе планирования, `prior_node_runs`/`prior_evidence` для
подстановки РЕАЛЬНОГО значения на этапе исполнения. Ни `GraphPlanner`, ни
`GraphExecutor`, ни `RunInputs` этой сессией не менялись — план сборки набора
документов ничем не отличается от любого другого графа с уже доступными
листьями."""
from __future__ import annotations

from datetime import datetime, timezone

from finengine.contracts import (
    Basis,
    DocumentCandidate,
    EntityIdentity,
    ExtractedMetric,
    NodeKey,
    NodeRun,
    RevisionInfo,
    ScalarFact,
)
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.atoms.norm_basis import compare_basis
from finengine.skills.atoms.verify_identity_cross import _verify_pair
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import AvailableIndex, AvailableLeaf, GraphPlanner
from finengine.skills.spec.target import Target


def _candidate_leaf(doc_key: str, candidate: DocumentCandidate) -> tuple[NodeKey, DocumentCandidate]:
    """Один пред-известный лист графа — тот же `atom_name="case.evidence"`,
    которым уже пользуется `tests/planner/test_available_leaf_reuse.py` для
    «факт уже установлен в `Case`, второй раз производить не нужно»."""
    node_key = NodeKey(
        atom_name="case.evidence", atom_version="1.0.0", args_fingerprint=f"concept=DocumentCandidate:{doc_key}",
    )
    return node_key, candidate


def _run_with_candidates(registry, target, root_extra, candidates: dict[str, DocumentCandidate]):
    available = AvailableIndex()
    prior_node_runs: dict[NodeKey, NodeRun] = {}
    prior_evidence: dict[NodeKey, tuple] = {}
    now = datetime.now(timezone.utc)
    for doc_key, candidate in candidates.items():
        node_key, value = _candidate_leaf(doc_key, candidate)
        available.put("DocumentCandidate", AvailableLeaf(node_key=node_key, evidence_ids=(value.document_id,)),
                      concept=doc_key, entity_ref=target.entity_ref, period_ref=target.period_ref,
                      basis=target.basis)
        prior_node_runs[node_key] = NodeRun(
            node_key=node_key, run_id="run-1", status="succeeded", sandbox_profile="compute_no_network",
            produced_evidence_ids=[value.document_id], started_at=now, finished_at=now,
        )
        prior_evidence[node_key] = (value,)

    planner = GraphPlanner(registry=registry, policy=_dummy_policy())
    result, node_args = planner.plan_ex(target, available=available, root_extra=root_extra)
    assert result.feasibility.feasible, result.feasibility.reason

    executor = GraphExecutor(registry=registry)
    execution = executor.run(
        result.graph, node_args, run_id="run-1", inputs=RunInputs(),
        prior_node_runs=prior_node_runs, prior_evidence=prior_evidence,
    )
    return result, execution


def _dummy_policy():
    from finengine.contracts import PlanPolicy
    return PlanPolicy(policy_version="1.0.0")


def _entity(name: str) -> EntityIdentity:
    return EntityIdentity(entity_id=f"ent-{name.lower()}", canonical_name=name, raw_name=name)


def test_report_and_press_release_of_the_same_issuer_assemble_end_to_end():
    registry = build_registry(profile_id="ifrs_nf_ru")
    severstal = _entity("Северсталь")
    candidates = {
        "report": DocumentCandidate(
            document_id="doc-report", document_version="v1", entity=severstal,
            period_ref="FY2024", doc_type="report",
        ),
        "press_release": DocumentCandidate(
            document_id="doc-press", document_version="v1", entity=severstal,
            period_ref="FY2024", doc_type="press_release",
        ),
    }
    target = Target(output_type="DocumentSet", entity_ref="case-1", period_ref="FY2024")
    root_extra = {
        "candidate_keys": "report,press_release",
        "expected_entity_refs": "Северсталь",
        "required_entities": "1",
        "required_doc_types": "report,press_release",
    }

    result, execution = _run_with_candidates(registry, target, root_extra, candidates)

    assert execution.succeeded
    document_set = execution.evidence_by_key[result.graph.root][0]
    assert document_set.completeness == "complete"
    assert {m.doc_type for m in document_set.members} == {"report", "press_release"}
    assert document_set.rejected == []


def test_an_unrelated_third_document_is_rejected_not_silently_joined_end_to_end():
    registry = build_registry(profile_id="ifrs_nf_ru")
    severstal, unrelated = _entity("Северсталь"), _entity("Совершенно Другая Компания")
    candidates = {
        "report": DocumentCandidate(
            document_id="doc-report", document_version="v1", entity=severstal,
            period_ref="FY2024", doc_type="report",
        ),
        "press_release": DocumentCandidate(
            document_id="doc-press", document_version="v1", entity=severstal,
            period_ref="FY2024", doc_type="press_release",
        ),
        "stray": DocumentCandidate(
            document_id="doc-stray", document_version="v1", entity=unrelated,
            period_ref="FY2024", doc_type="report",
        ),
    }
    target = Target(output_type="DocumentSet", entity_ref="case-1", period_ref="FY2024")
    root_extra = {
        "candidate_keys": "report,press_release,stray",
        "expected_entity_refs": "Северсталь",
        "required_entities": "1",
        "required_doc_types": "report,press_release",
    }

    result, execution = _run_with_candidates(registry, target, root_extra, candidates)

    assert execution.succeeded
    document_set = execution.evidence_by_key[result.graph.root][0]
    assert document_set.completeness == "complete"
    assert {m.document_id for m in document_set.members} == {"doc-report", "doc-press"}
    assert len(document_set.rejected) == 1
    assert document_set.rejected[0].document_id == "doc-stray"
    assert document_set.rejected[0].reason == "entity_mismatch"


def test_norm_basis_blocks_comparison_between_original_and_restated_revision():
    """Обязательный тест карточки: «norm.basis блокирует некорректное
    сравнение» — атом Среза 6a переиспользуется НЕИЗМЕНЁННЫМ, `revision`
    (уже существовавшая ось `Basis`, Фаза 0) теперь реально различает
    исходную и исправленную стороны многодокументной сверки."""
    original = Basis(standard="ifrs", perimeter="consolidated", revision="original")
    restated = Basis(standard="ifrs", perimeter="consolidated", revision="restated")

    comparison = compare_basis(original, restated)

    assert comparison.status == "not_comparable"
    assert "revision" in comparison.reason


def test_verify_cross_core_preserves_both_conflicting_facts_from_two_documents():
    """Обязательный тест карточки: «verify.cross сохраняет оба конфликтующих
    факта». Ядро сравнения (`_verify_pair`, Срез 6a) переиспользуется как
    есть для ПАРЫ, полученной из ДВУХ документов (отчёт и пресс-релиз), а не
    только из извлечения/расчёта одного прогона — реальное подключение этой
    пары как обязательного гейта графа для межд документной сверки не входит
    в эту сессию (см. Nedodelano.md, Срез 8): здесь проверяется контракт
    сравнения, который такое подключение будет использовать."""
    from_report = ExtractedMetric(
        concept="SALES_REVENUE", period_ref="FY2024",
        scalar_fact=ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0",
            source_ref="doc-report#p1", document_version="doc-report-v1",
            concept="SALES_REVENUE", period_ref="FY2024", value=1000.0, unit="RUB_MLN",
        ),
    )
    from_press_release = ExtractedMetric(
        concept="SALES_REVENUE", period_ref="FY2024",
        scalar_fact=ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0",
            source_ref="doc-press#p1", document_version="doc-press-v1",
            concept="SALES_REVENUE", period_ref="FY2024", value=950.0, unit="RUB_MLN",
        ),
    )

    finding = _verify_pair(
        concept="SALES_REVENUE", evidence_a=from_report, evidence_b=from_press_release,
        tolerance=0.01, check_kind="cross", atom_name="verify.cross",
    )

    assert finding.status == "fail"
    # Ни одна из двух сторон не была изменена/уничтожена сравнением — оба
    # факта остаются полноценными, адресуемыми доказательствами своего
    # документа.
    assert from_report.scalar_fact.value == 1000.0
    assert from_press_release.scalar_fact.value == 950.0
    assert from_report.scalar_fact.document_version == "doc-report-v1"
    assert from_press_release.scalar_fact.document_version == "doc-press-v1"


def test_original_and_restated_revision_info_is_carried_through_the_set():
    """`RevisionInfo` (Срез 8) проходит через `DocumentCandidate` в
    `DocumentSetMember` неизменным — сборка набора не решает вопрос ревизии
    заново, только переносит уже установленный факт."""
    registry = build_registry(profile_id="ifrs_nf_ru")
    severstal = _entity("Северсталь")
    candidates = {
        "original": DocumentCandidate(
            document_id="doc-2023", document_version="v1", entity=severstal, period_ref="FY2023",
        ),
        "restated": DocumentCandidate(
            document_id="doc-2024", document_version="v1", entity=severstal, period_ref="FY2023",
            revision=RevisionInfo(revision_id="rev-1", document_id="doc-2024",
                                   revision_kind="restated", supersedes="doc-2023"),
        ),
    }
    target = Target(output_type="DocumentSet", entity_ref="case-1", period_ref="FY2023")
    root_extra = {"candidate_keys": "original,restated", "required_entities": "1"}

    result, execution = _run_with_candidates(registry, target, root_extra, candidates)

    document_set = execution.evidence_by_key[result.graph.root][0]
    assert len(document_set.members) == 2
    by_id = {m.document_id: m for m in document_set.members}
    assert by_id["doc-2023"].revision is None
    assert by_id["doc-2024"].revision.revision_kind == "restated"
    assert by_id["doc-2024"].revision.supersedes == "doc-2023"


def test_id_revision_flows_through_the_real_planner_and_executor():
    """`id.revision` — не только вызываемая напрямую функция (`tests/atoms/
    test_id_revision.py`), но и настоящий зарегистрированный узел графа: сам
    по себе не нуждается в `RunPassport` (как `read.quote`), первый проход
    уже финальный."""
    import json

    def _fake_revision_executor(args, ctx):
        from finengine.skills.atoms.id_revision import detect_revision
        candidates = json.loads(args.extra.get("candidate_prior_documents") or "[]")

        def _fake_llm(prompt, system):  # noqa: ARG001
            return {
                "is_revision": True, "revision_kind": "restated",
                "supersedes_document_id": "doc-2023", "reason": "исправление ошибки в учёте резервов",
            }

        info = detect_revision(
            ctx.inputs.document_text, document_id=args.entity_ref or ctx.run_id,
            candidate_prior_documents=candidates, llm_json_call=_fake_llm,
        )
        return [info] if info is not None else []

    registry = build_registry(profile_id="ifrs_nf_ru", revision_executor=_fake_revision_executor)
    text = "Сравнительные данные за 2023 год были пересчитаны (restated) в связи с исправлением ошибки в учёте резервов."
    goal = Target(output_type="RevisionInfo", entity_ref="doc-2024", period_ref="p-2024-fy")

    planner = GraphPlanner(registry=registry, policy=_dummy_policy())
    result, node_args = planner.plan_ex(
        goal, root_extra={"candidate_prior_documents": json.dumps([{"document_id": "doc-2023"}])},
    )
    assert result.feasibility.feasible

    executor = GraphExecutor(registry=registry)
    execution = executor.run(result.graph, node_args, run_id="run-1", inputs=RunInputs(document_text=text))

    assert execution.succeeded
    revision = execution.evidence_by_key[result.graph.root][0]
    assert revision.revision_kind == "restated"
    assert revision.supersedes == "doc-2023"
