"""A1.4 шаг 9 / A1.7 / A1.11.6: обязательные контрольные этапы — явные узлы
графа ДО исполнения, а не проверки, которые можно обойти, если инструкция
модели про них забыла."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner


def test_gate_is_inserted_between_producer_and_consumer(atom, target, policy):
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("ASSETS", "LIABILITIES"), requires=("DOC",)),
        atom("calc.ratio", ("RATIO",), requires=("ASSETS", "LIABILITIES"), mandatory_gates=("verify.arithmetic",)),
        # Гейт производит НЕ "RATIO" (это бы завело второго кандидата в
        # producers_of("RATIO") и превратило прямой запрос RATIO в ложный
        # ChoicePoint) — он производит верификационную находку ПРО RATIO,
        # тем же пространством имён, что golden-фикстур planner-02
        # (`VerificationFinding:CURRENT_RATIO`). Гейт находится по имени через
        # `atom.mandatory_gates`, не через реестр по концепту.
        atom("verify.arithmetic", ("Verified:RATIO",)),
        atom("analyze.consume", ("VERDICT",), requires=("RATIO",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("VERDICT"))

    assert result.feasibility.feasible
    verdict_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "analyze.consume")
    gate_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "verify.arithmetic")
    calc_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "calc.ratio")

    # Потребитель зависит от ГЕЙТА, не от сырого вывода calc.ratio напрямую —
    # получить RATIO в обход verify.arithmetic невозможно по построению графа.
    assert verdict_node.requires == [gate_node.node_key]
    assert gate_node.requires == [calc_node.node_key]


def test_requesting_the_gated_concept_directly_still_returns_the_gate(atom, target, policy):
    """Если корень сам хочет `RATIO` напрямую, он получает узел ПОСЛЕ гейта —
    нельзя запросить обход проверки, назвав концепт корнем."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("ASSETS", "LIABILITIES"), requires=("DOC",)),
        atom("calc.ratio", ("RATIO",), requires=("ASSETS", "LIABILITIES"), mandatory_gates=("verify.arithmetic",)),
        atom("verify.arithmetic", ("Verified:RATIO",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("RATIO"))

    assert result.graph.root.atom_name == "verify.arithmetic"


def test_gate_can_require_a_second_independent_input(atom, target, policy):
    """Срез 6a: гейт объявляет СОБСТВЕННЫЙ `requires` — второй независимый
    источник, которого цепочка `[current]` сама по себе не даёт (`verify.
    identity`/`verify.cross`/`verify.citation`, 16a, требуют «сравнить два
    независимо полученных значения» — второе значение не может прийти через
    предыдущий узел ТОЙ ЖЕ цепочки, у него другой источник)."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("ASSETS",), requires=("DOC",)),
        atom("extract.other_source", ("ASSETS_ALT",), requires=("DOC",)),
        atom("calc.ratio", ("RATIO",), requires=("ASSETS",), mandatory_gates=("verify.cross",)),
        atom("verify.cross", ("Verified:RATIO",), requires=("ASSETS_ALT",)),
        atom("analyze.consume", ("VERDICT",), requires=("RATIO",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("VERDICT"))

    assert result.feasibility.feasible
    gate_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "verify.cross")
    calc_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "calc.ratio")
    alt_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "extract.other_source")

    assert calc_node.node_key in gate_node.requires
    assert alt_node.node_key in gate_node.requires
    assert len(gate_node.requires) == 2


def test_gate_without_its_own_requires_still_gets_only_the_chain_predecessor(atom, target, policy):
    """Обратная совместимость: гейт без собственных `requires`/`dynamic_
    requires` (сегодняшний `verify.arithmetic`) получает ровно `[current]`,
    как и до расширения механики — ничего не сломано для уже работающих
    гейтов."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("ASSETS", "LIABILITIES"), requires=("DOC",)),
        atom("calc.ratio", ("RATIO",), requires=("ASSETS", "LIABILITIES"), mandatory_gates=("verify.arithmetic",)),
        atom("verify.arithmetic", ("Verified:RATIO",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("RATIO"))

    gate_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "verify.arithmetic")
    calc_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "calc.ratio")
    assert gate_node.requires == [calc_node.node_key]


def test_two_independent_consumers_of_the_same_gated_producer_both_get_the_gate(atom, target, policy):
    """Регрессия (Срез 6a): второй, независимый запрос УЖЕ раскрытого
    производителя с `mandatory_gates` обязан получить тот же ПОСЛЕ-гейтовый
    ключ, что и первый, а не сырой ключ атома-производителя в обход гейта.
    Раньше `_expand_candidate` на найденном в `state.nodes` узле возвращал
    node_key САМОГО производителя (до применения `_apply_mandatory_gates`),
    и второй потребитель тихо обходил обязательную проверку — ровно тот
    случай, что `calc.formula`/`analyze.full_bundle` оба нуждаются в одном
    и том же `read.value|X`, реально воспроизведённый на синтетическом
    реестре без реальных атомов Среза 6a."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("ASSETS",), requires=("DOC",), mandatory_gates=("verify.arithmetic",)),
        atom("verify.arithmetic", ("Verified:ASSETS",)),
        atom("consumer.a", ("OUT_A",), requires=("ASSETS",)),
        atom("consumer.b", ("OUT_B",), requires=("ASSETS",)),
        atom("root.verdict", ("VERDICT",), requires=("OUT_A", "OUT_B")),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("VERDICT"))

    assert result.feasibility.feasible
    gate_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "verify.arithmetic")
    raw_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "extract.scalar")
    consumer_a = next(n for n in result.graph.nodes if n.node_key.atom_name == "consumer.a")
    consumer_b = next(n for n in result.graph.nodes if n.node_key.atom_name == "consumer.b")

    assert consumer_a.requires == [gate_node.node_key]
    assert consumer_b.requires == [gate_node.node_key]
    # Сырой узел производителя по-прежнему существует в графе (гейт от него
    # зависит), просто ни один ПОТРЕБИТЕЛЬ не смеет требовать его напрямую.
    assert raw_node.node_key not in consumer_a.requires
    assert raw_node.node_key not in consumer_b.requires


def test_gate_registered_nowhere_is_a_configuration_error(atom, target, policy):
    """Атом объявляет обязательный этап, которого нет в реестре — это брак
    самого реестра (несогласованные контракты), и он обязан быть виден
    громко, а не тихо пропущен."""
    import pytest

    registry = AtomRegistry([
        atom("calc.ratio", ("RATIO",), mandatory_gates=("verify.missing",)),
    ])
    with pytest.raises(ValueError, match="verify.missing"):
        GraphPlanner(registry=registry, policy=policy).plan(target("RATIO"))
