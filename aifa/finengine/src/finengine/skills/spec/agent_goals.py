"""Проверка частей цели агента перед исполнением сценария (ADR-0060)."""
from __future__ import annotations

from typing import cast, get_args

from finengine.contracts import GoalClause, TargetNode
from finengine.contracts.intent import IntentKind
from finengine.roles.access import EXTRACTION_ROLES
from finengine.skills.atoms.ad_hoc_concepts import (
    concept_spec_from_payload, formula_rejection_reason, formula_spec_from_payload,
)
from finengine.skills.metric_schema import registry_for_passport

GOAL_FIELDS = frozenset({"ref", "scenario", "concepts", "period", "role", "depends_on", "metric", "formula"})
_WITH_TARGETS = ("extract_registered_metric", "calculate_registered_metric")
_NEEDS_CONCEPTS = (*_WITH_TARGETS, "explain_change")
_SCENARIO_NAMES = frozenset(get_args(IntentKind))


def goal_clause(item: dict, *, where: str, raw_query: str) -> GoalClause:
    """Часть цели из объекта хода. Нарушение формы — `ValueError` с местом (`goals[0]`) и причиной."""
    unknown = sorted(set(item) - GOAL_FIELDS)
    if unknown:
        raise ValueError(f"{where}: лишние поля {unknown}; поля части цели — {sorted(GOAL_FIELDS)}")
    ref = _text(item.get("ref"))
    scenario = _text(item.get("scenario"))
    if not ref:
        raise ValueError(f"{where}: нужен ref — короткое имя части (g1, g2…), на него ссылается depends_on")
    if scenario not in _SCENARIO_NAMES:
        raise ValueError(f"{where}: scenario={item.get('scenario')!r} — не сценарий службы (references/scenarios.md)")
    concepts = [_text(value).upper() for value in _list(item.get("concepts"), f"{where}.concepts") if _text(value)]
    depends_on = [_text(value) for value in _list(item.get("depends_on"), f"{where}.depends_on") if _text(value)]
    parameters: dict = {}
    unresolved: list[str] = []
    if scenario == "explain_change" and concepts:
        parameters["analysis_concepts"] = concepts
    if scenario == "extract_ad_hoc_metric":
        spec = concept_spec_from_payload(_object(item.get("metric"), f"{where}.metric"), fallback_term="")
        if spec is None:
            raise ValueError(
                f"{where}: для extract_ad_hoc_metric нужен metric с name_ru — название показателя вне реестра"
            )
        parameters["ad_hoc_concept"] = spec.model_dump(mode="json")
        unresolved = [spec.term]
    if scenario == "calculate_ad_hoc_metric":
        payload = _object(item.get("formula"), f"{where}.formula")
        formula = formula_spec_from_payload(payload, fallback_term="")
        if formula is None:
            raise ValueError(f"{where}: формула не принята — {formula_rejection_reason(payload)}")
        parameters["ad_hoc_formula"] = formula.model_dump(mode="json")
        unresolved = [formula.term]
    return GoalClause(
        goal_ref=ref, raw_query=raw_query, intent_kind=cast(IntentKind, scenario),
        targets=[TargetNode(concept=concept, output_kind="scalar") for concept in concepts]
        if scenario in _WITH_TARGETS else [],
        period_ref=_text(item.get("period")) or None, role=_text(item.get("role")).lower() or None,
        parameters=parameters, unresolved=unresolved, depends_on=depends_on,
    )


def registry_problem(clause: GoalClause, passport) -> str | None:
    """Коды показателей и роль части — по реестру профиля этого отчёта; `None` — часть годна."""
    concepts = [target.concept for target in clause.targets] or list(clause.parameters.get("analysis_concepts") or [])
    if clause.intent_kind in _NEEDS_CONCEPTS and not concepts:
        return f"для {clause.intent_kind} нужны concepts — коды из «Реестра показателей»"
    if clause.role is not None and clause.role not in EXTRACTION_ROLES:
        return f"роль {clause.role!r} не из списка: {', '.join(EXTRACTION_ROLES)}"
    if clause.intent_kind == "form" and clause.role is None:
        return f"для form нужна role — одна из: {', '.join(EXTRACTION_ROLES)}"
    if passport is None or not concepts:
        return None
    registry = registry_for_passport(passport)
    calculated = {enum for enum, *_ in registry["calculated"]}
    known = calculated | {metric["enum"] for metric in registry["extractable"]}
    unknown = [concept for concept in concepts if concept not in known]
    if unknown:
        return (
            f"кодов {unknown} нет в «Реестре показателей» этого отчёта: возьми код оттуда; показателя в реестре нет — "
            "extract_ad_hoc_metric (metric) или calculate_ad_hoc_metric (formula)"
        )
    not_calculated = [concept for concept in concepts if concept not in calculated]
    if clause.intent_kind == "calculate_registered_metric" and not_calculated:
        return f"{not_calculated} — не рассчитываемые показатели реестра: их получает extract_registered_metric"
    return None


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _list(value, where: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where} — список строк")
    return value


def _object(value, where: str) -> object:
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{where} — объект")
    return value
