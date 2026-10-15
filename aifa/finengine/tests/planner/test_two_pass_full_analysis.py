"""A1.5 / A1.11.9: двухпроходное раскрытие. Первый проход останавливается на
границе, где состав зависимостей зависит от классификации; второй — с уже
известным `RunPassport` — продолжает раскрытие ТОЙ ЖЕ корневой вершины, не
строит новый план."""
from __future__ import annotations

from finengine.contracts import ClassificationResult, EntityIdentity, RunPassport
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner

_PASSPORT = RunPassport(
    goal_ir_id="goal-1",
    classification=ClassificationResult(
        profile_id="ifrs_nf_ru", standard="ifrs", company_type="non_financial", language="ru",
        reason="test fixture",
    ),
    entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
    registry_version="registry-1", methodology_version="methodology-1",
    model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
)


def _registry(atom):
    return AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("identify.classify", ("RunPassport",), requires=("DOC",)),
        atom(
            "extract.scalar", ("CAPITAL_ADEQUACY",), needs_run_passport=True,
            dynamic_requires=lambda node_args, passport: (
                ("BANK_ONLY_INPUT",) if passport.classification.company_type == "bank" else ()
            ),
        ),
        atom("extract.bank_only", ("BANK_ONLY_INPUT",), requires=("DOC",)),
    ])


def test_first_pass_stops_at_the_classification_boundary(atom, target, policy):
    result = GraphPlanner(registry=_registry(atom), policy=policy).plan(target("CAPITAL_ADEQUACY"))

    assert result.pending_run_passport is True
    assert result.feasibility.feasible, "первый проход не недостижим — он просто ещё не завершён"
    names = {n.node_key.atom_name for n in result.graph.nodes}
    assert names == {"ingest.document", "identify.classify", "extract.scalar"}
    gated_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "extract.scalar")
    classify_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "identify.classify")
    assert gated_node.requires == [classify_node.node_key], (
        "до RunPassport узел не раскрывает свои dynamic_requires — только зависимость от identify.classify"
    )


def test_second_pass_continues_the_same_root_with_the_resolved_dependency(atom, target, policy):
    planner = GraphPlanner(registry=_registry(atom), policy=policy)
    goal = target("CAPITAL_ADEQUACY")

    first = planner.plan(goal)
    second = planner.plan(goal, run_passport=_PASSPORT)

    assert second.pending_run_passport is False
    assert second.graph.root == first.graph.root, "тот же идентификатор корневой вершины (A1.11.9)"
    names = {n.node_key.atom_name for n in second.graph.nodes}
    # non_financial в фикстуре паспорта — BANK_ONLY_INPUT/extract.bank_only не раскрываются.
    assert "extract.bank_only" not in names
    gated_node = next(n for n in second.graph.nodes if n.node_key.atom_name == "extract.scalar")
    classify_node = next(n for n in second.graph.nodes if n.node_key.atom_name == "identify.classify")
    assert gated_node.requires == [classify_node.node_key]


def test_second_pass_for_a_bank_pulls_in_the_bank_only_branch(atom, target, policy):
    bank_passport = _PASSPORT.model_copy(update={
        "classification": _PASSPORT.classification.model_copy(update={"company_type": "bank"}),
    })
    result = GraphPlanner(registry=_registry(atom), policy=policy).plan(
        target("CAPITAL_ADEQUACY"), run_passport=bank_passport,
    )

    assert result.feasibility.feasible
    names = {n.node_key.atom_name for n in result.graph.nodes}
    assert "extract.bank_only" in names
