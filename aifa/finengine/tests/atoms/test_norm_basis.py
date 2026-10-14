"""`norm.basis` — критерий готовности атома (16a):

1. Консолидированная/отдельная, валовая/чистая и иные базы не смешиваются
   без явного правила преобразования.
2. В 100% конфликтных кейсов несовместимая база обнаруживается до расчёта.
3. Каждый преобразованный факт хранит исходный и целевой базис и правило
   перехода (здесь — `BasisComparison.bridge_rule`, когда мост применился).
4. Если корректное преобразование невозможно, результат остаётся
   недоступным (`not_comparable`), а не оценивается приблизительно.

Сценарии из карточки: IFRS против РСБУ, группа против материнской компании,
пересмотренное против исходного, RUB против USD, разные определения долга
(последнее — вне периметра numeric-осей `Basis`, определение показателя, не
базис — не моделируется здесь, см. отчёт по атому).
"""
from __future__ import annotations

from finengine.contracts import Basis
from finengine.skills.atoms.norm_basis import compare_basis


def test_identical_basis_on_every_axis_is_comparable():
    a = Basis(standard="ifrs", perimeter="consolidated", currency="RUB")
    b = Basis(standard="ifrs", perimeter="consolidated", currency="RUB")
    result = compare_basis(a, b)
    assert result.status == "comparable"


def test_ifrs_vs_rsbu_without_a_documented_bridge_is_not_comparable():
    a = Basis(standard="ifrs")
    b = Basis(standard="rsbu")
    result = compare_basis(a, b)
    assert result.status == "not_comparable"
    assert result.bridge_rule is None


def test_group_vs_parent_perimeter_is_not_comparable():
    a = Basis(perimeter="group")
    b = Basis(perimeter="parent")
    result = compare_basis(a, b)
    assert result.status == "not_comparable"


def test_restated_vs_original_revision_is_not_comparable():
    a = Basis(revision="original")
    b = Basis(revision="restated")
    result = compare_basis(a, b)
    assert result.status == "not_comparable"


def test_rub_vs_usd_currency_is_not_comparable_without_fx_bridge():
    a = Basis(currency="RUB")
    b = Basis(currency="USD")
    result = compare_basis(a, b)
    assert result.status == "not_comparable"


def test_axis_declared_on_only_one_side_is_not_asserted_comparable():
    a = Basis(gross_net="gross")
    b = Basis()  # вторая сторона не измерила эту ось вовсе
    result = compare_basis(a, b)
    assert result.status == "not_comparable"


def test_both_sides_missing_the_same_axis_does_not_block_comparability():
    a = Basis(standard="ifrs")
    b = Basis(standard="ifrs")
    result = compare_basis(a, b)
    assert result.status == "comparable"


def test_not_comparable_result_is_a_legitimate_answer_not_an_approximation():
    """Отсутствие моста не даёт приблизительного `adjusted` — только
    `not_comparable`, когда моста для КОНКРЕТНОЙ пары значений нет."""
    a = Basis(standard="us_gaap")
    b = Basis(standard="ind_as")
    result = compare_basis(a, b)
    assert result.status == "not_comparable"
    assert result.bridge_rule is None
