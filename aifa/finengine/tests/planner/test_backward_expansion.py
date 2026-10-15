"""A1.4: раскрытие идёт от корня вниз, рекурсивно, пока каждая ветвь не
упрётся в доступный лист. Расчётный показатель с рекурсивными зависимостями
(A1.10) — формула требует другую формулу, которая требует извлечённые факты."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import InputSpec, OutputSpec, Target


def _by_atom(nodes, name):
    return [n for n in nodes if n.node_key.atom_name == name]


def test_recursive_formula_dependency_pulls_in_its_own_inputs(atom, policy):
    """`NET_DEBT_EBITDA` требует `NET_DEBT` (сам расчётный) и `EBITDA`
    (извлекаемый) — планировщик обязан раскрыть ОБЕ ветви до листьев, не
    остановиться на первом уровне requires.

    `extract.scalar` производит ТРИ разных концепта — `OutputSpec(...,
    concept=...)` явно на каждый: голая-строка-конвенция фикстуры `atom()`
    даёт всем элементам `concept=None`, из-за чего три логических узла
    схлопнулись бы в один (см. `RETROFIT-REPORT-v9.md`, R3 test suite)."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom(
            "extract.scalar",
            tuple(OutputSpec(output_type="ScalarFact", concept=c) for c in ("TOTAL_DEBT", "CASH", "EBITDA")),
            requires=("DOC",),
        ),
        atom(
            "calc.net_debt", (OutputSpec(output_type="ScalarFact", concept="NET_DEBT"),),
            requires=(
                InputSpec(input_type="ScalarFact", concept="TOTAL_DEBT"),
                InputSpec(input_type="ScalarFact", concept="CASH"),
            ),
        ),
        atom(
            "calc.net_debt_ebitda", ("NET_DEBT_EBITDA",),
            requires=(
                InputSpec(input_type="ScalarFact", concept="NET_DEBT"),
                InputSpec(input_type="ScalarFact", concept="EBITDA"),
            ),
        ),
    ])
    target = Target(output_type="NET_DEBT_EBITDA", entity_ref="e1", period_ref="p1")
    result = GraphPlanner(registry=registry, policy=policy).plan(target)

    assert result.feasibility.feasible
    names = {n.node_key.atom_name for n in result.graph.nodes}
    assert names == {"ingest.document", "extract.scalar", "calc.net_debt", "calc.net_debt_ebitda"}
    # `extract.scalar` — параметризованный атом (по умолчанию `concept` входит
    # в `key_fields`): TOTAL_DEBT/CASH/EBITDA — ТРИ разных логических узла, не
    # один. Их физическое объединение в один вызов движка — работа
    # `GraphExecutor`/`BatchKey` (A2), не дедупликации планировщика (A1.4 шаг
    # 10 дедуплицирует ОДИНАКОВЫЙ `NodeKey`, а не разные концепты одного атома).
    scalar_nodes = _by_atom(result.graph.nodes, "extract.scalar")
    assert len(scalar_nodes) == 3
    ebitda_producer = next(n for n in scalar_nodes if n.produces == ["ScalarFact:EBITDA"]).node_key

    net_debt_node = _by_atom(result.graph.nodes, "calc.net_debt")[0]
    net_debt_ebitda_node = _by_atom(result.graph.nodes, "calc.net_debt_ebitda")[0]
    assert net_debt_node.node_key in net_debt_ebitda_node.requires
    assert ebitda_producer in net_debt_ebitda_node.requires
    assert all(n.requires == [_by_atom(result.graph.nodes, "ingest.document")[0].node_key] for n in scalar_nodes)


def test_point_request_does_not_expand_the_full_registry(atom, target, policy):
    """Сквозной приёмочный сценарий #3 (99-matrica.md): точечный запрос по
    одному показателю не запускает полный реестр — в графе нет узла,
    производящего показатель, который никто не запрашивал."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("SALES_REVENUE",), requires=("DOC",)),
        atom("extract.other", ("UNRELATED_METRIC",), requires=("DOC",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("SALES_REVENUE"))

    names = {n.node_key.atom_name for n in result.graph.nodes}
    assert names == {"ingest.document", "extract.scalar"}
    assert "extract.other" not in names
