"""A1.4 шаг 2-3 / A1.11.4: если требуемый узел уже присутствует среди
доступных доказательств с совместимым `NodeKey`, его производитель НЕ
добавляется в граф повторно — ветвь заканчивается на уже доступном листе.
Сквозной приёмочный сценарий #11 (99-matrica.md): следующий вопрос в чате
переиспользует уже проверенные факты."""
from __future__ import annotations

from finengine.contracts import NodeKey
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import AvailableIndex, AvailableLeaf, GraphPlanner


def test_available_scalar_fact_short_circuits_its_producer(atom, target, policy):
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("REVENUE",), requires=("DOC",)),
    ])
    already_known = NodeKey(atom_name="case.evidence", atom_version="1.0.0",
                             args_fingerprint="concept=REVENUE;entity=e1;period=p1")
    available = AvailableIndex()
    available.put("REVENUE", AvailableLeaf(node_key=already_known, evidence_ids=("ev-1",)),
                  entity_ref="e1", period_ref="p1")

    result = GraphPlanner(registry=registry, policy=policy).plan(target("REVENUE"), available=available)

    assert result.feasibility.feasible
    assert result.graph.root == already_known
    names = {n.node_key.atom_name for n in result.graph.nodes}
    assert "extract.scalar" not in names, "производитель не должен строиться повторно поверх уже доступного факта"
    assert "ingest.document" not in names, "если сам факт уже есть, документ под него раскрывать незачем"


def test_leaf_identity_is_specific_to_entity_and_period(atom, target, policy):
    """Тот же концепт для ДРУГОЙ сущности/периода — другой узел (A1.2):
    доступность факта по Северстали не должна тихо подставиться под запрос
    по НЛМК."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("REVENUE",), requires=("DOC",)),
    ])
    known = NodeKey(atom_name="case.evidence", atom_version="1.0.0", args_fingerprint="entity=severstal")
    available = AvailableIndex()
    available.put("REVENUE", AvailableLeaf(node_key=known), entity_ref="severstal", period_ref="p1")

    result = GraphPlanner(registry=registry, policy=policy).plan(
        target("REVENUE", entity_ref="nlmk", period_ref="p1"), available=available,
    )

    names = {n.node_key.atom_name for n in result.graph.nodes}
    assert "extract.scalar" in names, "другая сущность обязана раскрыть своего производителя, а не переиспользовать чужой факт"
