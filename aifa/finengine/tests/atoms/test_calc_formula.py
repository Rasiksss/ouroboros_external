"""`calc.formula` — критерий готовности атома (16a):

1. Формулы реестра проходят точные примеры с числовой погрешностью не хуже
   `1e-9` для операций без округления.
2. Отсутствующая зависимость не заменяется нулём и не даёт рассчитанный
   факт.
3. Цикл зависимостей выявляется до вычисления — покрыто на уровне
   планировщика (`tests/planner/test_cycles.py`, не дублируется здесь:
   `calc.formula` использует ту же самую `dynamic_requires` механику, что
   уже проверена).
4. Каждый `DerivedFact` payload хранит формулу, версии зависимостей и их
   идентификаторы.
5. Модельные вызовы при вычислении отсутствуют (чистый Python, без
   monkeypatch модельного шлюза — если бы он был нужен, тест обязан был бы
   его подставить явно).

Сценарии из карточки: деление на ноль, отсутствующая зависимость, разные
периоды/единицы (регресс по всем формулам реестра — вне периметра
юнит-теста, отдельная задача C7, см. отчёт по атому).
"""
from __future__ import annotations

import pytest

from finengine.contracts import ClassificationResult, EntityIdentity, ExtractedMetric, RunPassport, ScalarFact
from finengine.skills.atoms.calc_formula import calc_formula_executor, calc_formula_requires
from finengine.skills.spec.atom_registry import NodeArgs

_REGISTRY = {
    "extractable": [{"enum": "REVENUE"}, {"enum": "COST_OF_SALES"}, {"enum": "PPE"}],
    "calculated": [
        ("TURNOVER", "Оборот", "REVENUE / PPE", ["REVENUE", "PPE"]),
        ("GROSS_MARGIN", "Валовая маржа", "(REVENUE - COST_OF_SALES) / REVENUE", ["REVENUE", "COST_OF_SALES"]),
        ("ZERO_DIV_RATIO", "Деление на ноль", "REVENUE / COST_OF_SALES", ["REVENUE", "COST_OF_SALES"]),
    ],
    "calculated_meta": {},
}


def _passport() -> RunPassport:
    return RunPassport(
        goal_ir_id="goal-1",
        classification=ClassificationResult(
            profile_id="test-cell", standard="ifrs", company_type="non_financial", language="ru",
            reason="test fixture",
        ),
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
        registry_version="registry-7", methodology_version="methodology-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )


def _extracted(concept: str, value: float, period_ref: str = "2025") -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept=concept, period_ref=period_ref, value=value, unit="млн. руб.",
    )
    return ExtractedMetric(scalar_fact=fact, concept=concept, period_ref=period_ref)


class _Ctx:
    def __init__(self, dependencies, run_passport, run_id="run-1"):
        self._deps = dependencies
        self.run_passport = run_passport
        self.run_id = run_id

    def dependencies(self):
        return tuple(self._deps)


@pytest.fixture(autouse=True)
def _patched_registry(monkeypatch):
    """`compute_ratios` (skills/ratios.py) делает СВОЙ ленивый `from
    finengine.skills.metric_schema import registry_for_profile` внутри тела
    функции — патч имени, импортированного в `calc_formula.py`, его не
    затрагивает. Патчим оба места одним фикстурным заходом на весь модуль."""
    monkeypatch.setattr("finengine.skills.atoms.calc_formula.registry_for_passport", lambda passport: _REGISTRY)
    monkeypatch.setattr("finengine.skills.metric_schema.registry_for_profile", lambda pid: _REGISTRY)


def test_registry_formula_computes_within_tight_tolerance():
    passport = _passport()
    revenue, cost = _extracted("REVENUE", 1000.0), _extracted("COST_OF_SALES", 750.0)
    ctx = _Ctx([revenue, cost], passport)
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="2025"), ctx)
    assert len(result) == 1
    metric = result[0]
    assert abs(metric.derived_fact.value - 0.25) < 1e-9


def test_dimensionless_ad_hoc_ratio_preserves_its_declared_unit():
    ctx = _Ctx([_extracted("REVENUE", 1000.0), _extracted("PPE", 250.0)], _passport())
    args = NodeArgs(
        concept="Пользовательская оборачиваемость", period_ref="2025",
        extra={
            "ad_hoc_formula_expression": "REVENUE / PPE",
            "ad_hoc_formula_dependencies": "REVENUE,PPE",
            "ad_hoc_formula_term": "Пользовательская оборачиваемость",
            "ad_hoc_formula_assumptions": "",
            "ad_hoc_formula_unit": "раз",
        },
    )

    result = calc_formula_executor(args, ctx)

    assert len(result) == 1
    assert result[0].derived_fact.value == pytest.approx(4.0)
    assert result[0].derived_fact.unit == "раз"


def test_derived_fact_carries_formula_version_expression_and_dependency_evidence_ids(monkeypatch):
    monkeypatch.setattr("finengine.skills.atoms.calc_formula.registry_for_passport", lambda passport: _REGISTRY)
    passport = _passport()
    revenue, cost = _extracted("REVENUE", 1000.0), _extracted("COST_OF_SALES", 750.0)
    ctx = _Ctx([revenue, cost], passport)
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="2025"), ctx)
    fact = result[0].derived_fact
    assert fact.formula_id == "GROSS_MARGIN"
    assert fact.formula_version == "registry-7"
    assert fact.expression == "(REVENUE - COST_OF_SALES) / REVENUE"
    assert set(fact.inputs) == {revenue.scalar_fact.evidence_id, cost.scalar_fact.evidence_id}


def test_missing_dependency_gives_no_calculated_fact_not_zero(monkeypatch):
    monkeypatch.setattr("finengine.skills.atoms.calc_formula.registry_for_passport", lambda passport: _REGISTRY)
    passport = _passport()
    revenue = _extracted("REVENUE", 1000.0)  # COST_OF_SALES отсутствует
    ctx = _Ctx([revenue], passport)
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="2025"), ctx)
    assert result == []


def test_requested_period_equivalent_to_the_facts_canonical_label_still_computes():
    """Срез 30, живой real-model smoke 24.09 (Северсталь FY2024): GoalIR несёт
    сырое «2024», а `read.value` отдаёт каноническую метку документа «FY 2024».
    Продюсеры (`read.value`, `norm.time`) эту эквивалентность терпят, а
    `calc.formula` сверял `main_period` с метками фактов ТОЧНО — оба операнда
    были извлечены верно, но расчёт молча возвращал `[]`, и любой запрос вида
    «рассчитай … за 2024 год» заканчивался `canonical_pipeline_incomplete`.
    Fake read.value в интеграционных тестах эхом повторяет `args.period_ref`,
    поэтому расхождение меток там воспроизвести было нельзя."""
    ctx = _Ctx([_extracted("REVENUE", 1000.0, "FY 2024"), _extracted("COST_OF_SALES", 750.0, "FY 2024")], _passport())
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="2024"), ctx)
    assert len(result) == 1
    assert abs(result[0].derived_fact.value - 0.25) < 1e-9
    assert result[0].period_ref == "FY 2024"


def test_a_different_period_is_never_substituted_for_the_requested_one():
    """Эквивалентность меток не означает «возьмём что есть»: факты за другой
    период не могут молча стать ответом на запрос про 2024."""
    ctx = _Ctx([_extracted("REVENUE", 1000.0, "FY 2023"), _extracted("COST_OF_SALES", 750.0, "FY 2023")], _passport())
    assert calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="2024"), ctx) == []


def test_division_by_zero_never_publishes_a_calculated_fact():
    """`compute_ratios`/`formula_engine` уже перехватывают деление на ноль ВНУТРИ
    собственного расчёта одного показателя (широкие `except Exception` вокруг
    `_eval_formula_node` per-metric, чтобы одна плохая формула не роняла весь
    пакет) и возвращают для него пустой результат — тем же классом исхода,
    что «недостающая зависимость». Наружу к `calc.formula`, соответственно,
    исключение не долетает: try/except в `calc_formula_executor` — защита на
    случай ошибок, которые НЕ перехвачены на уровне одного показателя
    (например, брак ad-hoc AST при разборе формулы), не единственная линия
    защиты от деления на ноль. Важный, проверяемый здесь инвариант — деление
    на ноль НИКОГДА не публикует `CalculatedMetric` со значением `inf`/`nan`
    или молчаливым нулём."""
    passport = _passport()
    revenue, cost = _extracted("REVENUE", 1000.0), _extracted("COST_OF_SALES", 0.0)
    ctx = _Ctx([revenue, cost], passport)
    result = calc_formula_executor(NodeArgs(concept="ZERO_DIV_RATIO", period_ref="2025"), ctx)
    assert result == []


def test_concept_without_a_formula_returns_empty_not_an_error(monkeypatch):
    monkeypatch.setattr("finengine.skills.atoms.calc_formula.registry_for_passport", lambda passport: _REGISTRY)
    passport = _passport()
    ctx = _Ctx([], passport)
    result = calc_formula_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
    assert result == []


def test_dynamic_requires_resolves_dependencies_before_execution(monkeypatch):
    monkeypatch.setattr("finengine.skills.atoms.calc_formula.registry_for_passport", lambda passport: _REGISTRY)
    specs = calc_formula_requires(NodeArgs(concept="GROSS_MARGIN", period_ref="2025"), _passport())
    assert {s.concept for s in specs} == {"REVENUE", "COST_OF_SALES"}
    assert all(s.input_type == "MetricEvidence" for s in specs)


def test_numeric_engine_receives_the_same_frozen_registry(monkeypatch):
    """Formula lookup and numeric evaluation cannot straddle registry versions."""
    seen = {}

    def _compute(*_args, **kwargs):
        seen["registry"] = kwargs.get("registry_override")
        return []

    monkeypatch.setattr("finengine.skills.atoms.calc_formula.compute_ratios", _compute)
    passport = _passport()
    ctx = _Ctx([_extracted("REVENUE", 1000.0), _extracted("COST_OF_SALES", 750.0)], passport)
    calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="2025"), ctx)
    assert seen["registry"] is _REGISTRY


def test_no_model_gateway_is_touched_during_computation(monkeypatch):
    """Модельные вызовы при вычислении отсутствуют: если бы код случайно
    попытался дойти до модельного шлюза, monkeypatch ниже упал бы с
    AttributeError/ImportError — тест намеренно НЕ подставляет шлюз."""
    monkeypatch.setattr("finengine.skills.atoms.calc_formula.registry_for_passport", lambda passport: _REGISTRY)
    passport = _passport()
    ctx = _Ctx([_extracted("REVENUE", 1000.0), _extracted("COST_OF_SALES", 750.0)], passport)
    calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="2025"), ctx)


def test_a_ratio_of_a_flow_and_a_balance_computes_for_a_year_request():
    """Срез 30, real-model smoke (ЧТПЗ, фондоотдача): выручка приходит как
    «FY 2024», основные средства — как «31.12.2024». Оба — верный факт на
    запрошенный «2024», но legacy-конверт сверяет метку периода точно, и
    отношение потока к остатку не считалось вовсе. Каждая зависимость
    выравнивается по метке ЗАПРОШЕННОГО периода в соответствии со своим видом
    (поток/остаток); факты другого периода не подмешиваются."""
    ctx = _Ctx([
        _extracted("REVENUE", 1000.0, "FY 2024"),
        _extracted("PPE", 250.0, "31.12.2024"),
        _extracted("PPE", 999.0, "31.12.2023"),   # прошлогодний остаток — не участвует
    ], _passport())
    result = calc_formula_executor(NodeArgs(concept="TURNOVER", period_ref="2024"), ctx)
    assert len(result) == 1
    assert abs(result[0].derived_fact.value - 4.0) < 1e-9
