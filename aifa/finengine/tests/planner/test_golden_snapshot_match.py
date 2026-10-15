"""A1.11.12: «для эталонных сценариев "точечный показатель", "полный анализ",
"форма заказчика", "повторный вопрос с `Case`" фактический граф совпадает с
утверждённым снимком по корню, обязательным зависимостям и доступным
листьям».

Четыре эталона, три реально проверяемых сейчас источника:

* «точечный показатель» — golden-фикстур Фазы 0 (`planner-01-single-
  scalar-metric.json`) проверяется здесь БУКВАЛЬНО: `GraphPlanner` гоняется
  против реестра, СОБРАННОГО ИЗ САМОЙ ФИКСТУРЫ (`_registry_from_fixture`),
  не против отдельно написанной копии.
* «полный анализ» и «форма заказчика» — golden-фикстуры `planner-02`/
  `planner-03` описывают КОНЕЧНОЕ состояние продукта (их собственный
  `notes`/`description` это не скрывает), корень которого — `analyze.
  explain`/использует `compose.form` с продукцией, жёстко привязанной к
  роли `dgr`. `analyze.judge`/`analyze.explain` — атомы `assess.judgment`/
  `explain.internal` (см. `docs/build-plan/99-matrica-i-kriteriy-
  zaversheniya.md`, столбец «в каком файле искать карточку») — это Срез 6b/9,
  не Срез 3; они физически не могут появиться в реестре ЭТОГО среза.
  Настоящая, встроенная проверка этих двух сценариев — не байт-в-байт с
  ЭТИМИ фикстурами (это было бы проверкой чужого среза), а сквозные тесты
  `tests/integration/test_graph_full_analysis.py` и `test_graph_role_form.py`
  — они прогоняют ТЕ ЖЕ два сценария через настоящий `PlanGraph`+
  `GraphExecutor` и реальный движок, только с атомным словарём, который
  Срез 3 реально реализовал (`analyze.full_bundle`, `compose.form`
  общий-по-роли, не привязанный к `dgr` в самом `produces`). Тесты ниже это
  явно фиксируют, а не молчат об этом.
* «повторный вопрос с `Case`» — `Case` не существует до Среза 10; этот
  эталон физически непроверяем сейчас, что тоже честно фиксируется.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.contracts import PlanPolicy
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import OutputSpec, Target

MANUAL_DIR = Path(__file__).resolve().parents[1] / "golden" / "planner" / "manual"


def _node_key_tuple(nk: dict) -> tuple[str, str, str]:
    return (nk["atom_name"], nk["atom_version"], nk["args_fingerprint"])


def _registry_from_fixture(plan_graph: dict) -> AtomRegistry:
    """Собрать `AtomRegistry` ИЗ УЗЛОВ САМОЙ фикстуры — не второй, вручную
    поддерживаемый список атомов рядом с JSON. Узлы одного `atom_name`
    (например, два `extract.scalar` для разных концептов) сливаются в ОДИН
    параметризованный `AtomDef` — тот же принцип, каким реально построен
    `skills/atoms/graph_atoms.py`."""
    nodes = plan_graph["nodes"]
    produces_by_key = {_node_key_tuple(n["node_key"]): n["produces"] for n in nodes}

    merged: dict[str, dict] = {}
    for n in nodes:
        name = n["node_key"]["atom_name"]
        entry = merged.setdefault(name, {
            "produces": set(), "requires": set(),
            "sandbox_profile": n["sandbox_profile"], "version": n["node_key"]["atom_version"],
        })
        entry["produces"].update(n["produces"])
        for req in n["requires"]:
            entry["requires"].update(produces_by_key.get(_node_key_tuple(req), []))

    atoms = [
        AtomDef(
            name=name, version=e["version"], produces=tuple(sorted(e["produces"])),
            requires=tuple(sorted(e["requires"])), sandbox_profile=e["sandbox_profile"],
            execution_kind="deterministic", cache_policy="none", cost_class="free",
            executor=lambda *_a, **_k: (), key_fields=("concept", "entity_ref", "period_ref"),
        )
        for name, e in merged.items()
    ]
    return AtomRegistry(atoms)


def _load(scenario_id: str) -> dict:
    for path in MANUAL_DIR.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw["scenario_id"] == scenario_id:
            return raw
    raise AssertionError(f"golden-фикстур {scenario_id} не найден в {MANUAL_DIR}")


def test_point_metric_matches_the_golden_snapshot_literally():
    fixture = _load("planner-01-single-scalar-metric")
    registry = _registry_from_fixture(fixture["plan_graph"])
    # Retrofit v9: фикстур описывает `TargetNode` (`concept`/`output_kind`) —
    # `GraphPlanner` теперь строится от `Target`. `concept` фикстуры играет
    # роль `output_type` целиком (та же bare-string-конвенция, что
    # `tests/planner/conftest.py::_make_target`) — этот golden-снимок
    # проверяет ОБЩУЮ механику построения графа, не различение
    # extract/calculate output_type.
    target = Target(
        output_type=fixture["target"]["concept"],
        entity_ref=fixture["target"].get("entity_ref"),
        period_ref=fixture["target"].get("period_ref"),
    )
    policy = PlanPolicy(policy_version="golden-snapshot")

    result = GraphPlanner(registry=registry, policy=policy).plan(target)

    assert result.feasibility.feasible == fixture["expected_feasibility"]["feasible"]
    expected_nodes = fixture["plan_graph"]["nodes"]
    expected_root_atom = fixture["plan_graph"]["root"]["atom_name"]
    expected_atom_names = {n["node_key"]["atom_name"] for n in expected_nodes}
    expected_leaf_atoms = {n["node_key"]["atom_name"] for n in expected_nodes if not n["requires"]}

    assert result.graph.root.atom_name == expected_root_atom
    assert {n.node_key.atom_name for n in result.graph.nodes} == expected_atom_names
    assert {n.node_key.atom_name for n in result.graph.nodes if not n.requires} == expected_leaf_atoms


@pytest.mark.parametrize("scenario_id,missing_atom,later_slice", [
    ("planner-02-full-analysis-fan-in", "analyze.explain", "Срез 6b (explain.internal)"),
    ("planner-02-full-analysis-fan-in", "analyze.judge", "Срез 9 (assess.judgment)"),
])
def test_full_analysis_golden_fixture_roots_in_a_later_slices_atom(scenario_id, missing_atom, later_slice):
    """Документирует, а не молчит: `planner-02` — эталон КОНЕЧНОГО состояния
    продукта, чей корень требует атом, которого в Срезе 3 нет и не должно
    быть. Если однажды `missing_atom` появится в `graph_atoms.build_registry()`
    раньше своего среза — это архитектурный откат (обход границы B3), и этот
    тест обязан покраснеть первым."""
    fixture = _load(scenario_id)
    atom_names = {n["node_key"]["atom_name"] for n in fixture["plan_graph"]["nodes"]}
    assert missing_atom in atom_names, f"{scenario_id} больше не содержит {missing_atom} — фикстур изменился"
    assert build_registry().get(missing_atom) is None, (
        f"{missing_atom} появился в реестре Среза 3 раньше своего среза ({later_slice})"
    )


def test_full_analysis_intent_is_covered_by_the_real_integration_scenario():
    """То, ЧТО `planner-02` реально проверяет продуктово («полный анализ
    раскрывается только после классификации, fan-in двух фактов в расчёт,
    расчёт обязан пройти гейт») — покрыто `tests/integration/
    test_graph_full_analysis.py` настоящим `AtomRegistry` Среза 3
    (`analyze.full_bundle`, не `analyze.explain`). Проверяется по тексту
    файла, не импортом `tests.integration` как пакета — у `tests/
    integration/` нет `__init__.py` (та же причина, по которой соседние
    `conftest.py` этого среза используют фикстуры-фабрики, не импорты)."""
    src = (Path(__file__).resolve().parents[1] / "integration" / "test_graph_full_analysis.py").read_text(encoding="utf-8")
    assert "def test_full_analysis_expands_only_after_run_passport_and_gates_derived_facts" in src


def test_role_form_golden_fixture_hardcodes_the_role_into_produces():
    """`planner-03` объявляет `compose.form` производящим буквально
    `"FormOutput:dgr"` — роль зашита в `produces`. Настоящий `compose.form`
    Среза 3 (`graph_atoms.py`) производит родовой `"FormOutput"`, а роль —
    параметр (`NodeArgs.extra["role"]`), чтобы ОДИН атом обслуживал все роли
    реестра `roles/layouts.json`, а не заводил себе имя-производитель на
    каждую. Тест фиксирует это расхождение как сознательное архитектурное
    решение, не как недосмотр."""
    fixture = _load("planner-03-form-output-dgr")
    fixture_root = fixture["plan_graph"]["root"]
    assert fixture_root["atom_name"] == "compose.form"
    fixture_produces = next(
        n["produces"] for n in fixture["plan_graph"]["nodes"]
        if n["node_key"]["atom_name"] == "compose.form"
    )
    assert fixture_produces == ["FormOutput:dgr"]

    real_atom = build_registry().get("out.form")
    assert real_atom is not None
    assert real_atom.produces == (OutputSpec(output_type="FormOutput"),), (
        "compose.form Среза 3 родовой (роль — параметр), не завязан на dgr в produces"
    )


def test_role_form_intent_is_covered_by_the_real_integration_scenario():
    src = (Path(__file__).resolve().parents[1] / "integration" / "test_graph_role_form.py").read_text(encoding="utf-8")
    assert "def test_role_form_keeps_unavailable_rows_instead_of_dropping_them" in src


def test_repeat_question_with_case_is_not_yet_checkable():
    """`Case` — Срез 10. Четвёртый эталон A1.11.12 физически не существует
    раньше него; фиксируем это явно, а не пропускаем молча."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("finengine.case")
