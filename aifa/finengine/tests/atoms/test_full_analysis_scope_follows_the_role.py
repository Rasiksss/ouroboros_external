"""Срез 31, П2: полный анализ привязан к роли пользователя.

Владелец: «режим полного финансового анализа, который включает в себя извлечение
показателей, расчёт показателей, извлечение сегментной и консолидированной
информации, привязан к выбранной роли пользователя; у нас же есть реестры
показателей, в которых описаны, какие показатели каким ролям нужны».

Состав (`roles/scope.py`): показатели — `build_engine_scope(role)`; сегменты —
`role_should_include_segments`; состав группы — `role_should_include_consolidated_group`.
До Среза 31 роль в граф не доезжала, а сегменты и группа добавлялись любой роли.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from finengine.roles.scope import (
    build_engine_scope,
    role_should_include_consolidated_group,
    role_should_include_segments,
)
from finengine.skills.atoms.graph_atoms import _analyze_full_bundle_scope
from finengine.skills.spec.atom_registry import NodeArgs

_ROLES = ["other", "risks", "baifp", "die", "cib", "dgr"]


def _passport(company_type: str, profile_id: str):
    return SimpleNamespace(classification=SimpleNamespace(company_type=company_type, profile_id=profile_id))


def _scope(role: str | None, company_type: str, profile_id: str):
    extra = {"role": role} if role else {}
    return _analyze_full_bundle_scope(NodeArgs(extra=extra), _passport(company_type, profile_id))


def _types(scope) -> list[str]:
    return [spec.input_type for spec in scope]


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("company_type, profile_id", [("non_financial", "ifrs_nf_ru"), ("bank", "ifrs_bank_ru")])
def test_structures_are_included_exactly_when_the_role_policy_says_so(role, company_type, profile_id):
    scope = _scope(role, company_type, profile_id)
    has_segments = any(s.input_type == "ExtractedStructure" and s.concept == "segments" for s in scope)
    has_group = any(s.input_type == "EntityRelation" for s in scope)
    assert has_segments is role_should_include_segments(role, company_type)
    assert has_group is role_should_include_consolidated_group(role, company_type)


@pytest.mark.parametrize("role", _ROLES)
def test_the_metric_set_is_the_role_scope_from_the_registries(role):
    scope = _scope(role, "non_financial", "ifrs_nf_ru")
    _, expected = build_engine_scope(role, "non_financial", "ifrs_nf_ru", basis_full=False)
    got = {s.concept for s in scope if s.input_type == "MetricEvidence"}
    assert got == set(expected)


def test_roles_really_differ_in_what_they_need():
    """Иначе привязка к роли была бы формальностью. Раскладка ДГР заведена только
    для банков (`roles/layouts.json`), поэтому сравниваем на банковском профиле."""
    other = _scope("other", "bank", "ifrs_bank_ru")
    dgr = _scope("dgr", "bank", "ifrs_bank_ru")
    assert {s.concept for s in other if s.input_type == "MetricEvidence"} != {
        s.concept for s in dgr if s.input_type == "MetricEvidence"
    }
    assert "EntityRelation" in _types(other) and "EntityRelation" not in _types(dgr)


def test_a_missing_role_behaves_as_the_general_role():
    assert _scope(None, "non_financial", "ifrs_nf_ru") == _scope("other", "non_financial", "ifrs_nf_ru")


def test_a_concept_scope_from_the_request_still_narrows_the_metrics_but_keeps_the_structure_policy():
    passport = _passport("non_financial", "ifrs_nf_ru")
    scope = _analyze_full_bundle_scope(
        NodeArgs(extra={"role": "dgr", "requested_concepts": "SALES_REVENUE,NET_INCOME"}), passport,
    )
    assert {s.concept for s in scope if s.input_type == "MetricEvidence"} == {"SALES_REVENUE", "NET_INCOME"}
    assert "EntityRelation" not in _types(scope)
