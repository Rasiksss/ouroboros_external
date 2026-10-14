"""Definition resolver (16a, A1.6): чистые детерминированные функции,
которые материализуют `MetricDefinition`/`FormulaDefinition` — «specification
leaves» — либо из реестра ячейки (`registry_for_profile()`), либо из
`AdHocConceptSpec`/`AdHocFormulaSpec` текущего запроса. Ни сеть, ни модель
здесь не участвуют; `calc.formula.dynamic_requires` вызывает это ДО того, как
раскрывает зависимые `MetricEvidence` Targets (A1.6: «Handler не имеет права
внезапно обнаружить обязательную зависимость после начала собственной
работы»).

`version` — не отдельный новый источник истины: это `RunPassport.
registry_version`, уже зафиксированный на весь прогон (Срез 1) — то же самое
значение, что видит любой другой потребитель реестра в этом запуске, не
свежий пересчёт `skills.versioning.registry_fingerprint()` рядом с ним.
"""
from __future__ import annotations

from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec, FormulaDefinition, MetricDefinition, RunPassport


def _calc_entry_enum(entry: object) -> str:
    """Тот же приём, что `graph_atoms._calc_entry_enum` — разобранная строка
    `calculated` это кортеж `(enum, name_ru, formula, deps)`, не словарь."""
    return str(entry[0]).strip().upper() if isinstance(entry, (list, tuple)) and entry else ""


def _calc_entry_formula(entry: object) -> str:
    return str(entry[2]) if isinstance(entry, (list, tuple)) and len(entry) >= 3 else ""


def _calc_entry_deps(entry: object) -> tuple[str, ...]:
    deps = entry[3] if isinstance(entry, (list, tuple)) and len(entry) >= 4 else ()
    return tuple(str(d).strip().upper() for d in (deps or ()))


def _known_concepts(registry: dict) -> set[str]:
    return (
        {str(m["enum"]).strip().upper() for m in registry.get("extractable", [])}
        | {_calc_entry_enum(c) for c in registry.get("calculated", [])}
    ) - {""}


def resolve_metric_definition(
    concept: str, registry: dict, run_passport: RunPassport | None, *, ad_hoc: AdHocConceptSpec | None = None,
) -> MetricDefinition:
    """`MetricDefinition` для `norm.unit`/`norm.time`/`verify.bounds`.

    Концепт вне реестра И без `ad_hoc` не даёт `None` (в отличие от
    `resolve_formula_definition`, где "формулы нет" — законный исход) — у
    любого запрошенного показателя есть каноническая семантика по
    построению, даже если она пустая (норма — «схема её не задаёт», не
    «показателя не существует»): вызывающий `dynamic_requires`/атом уже
    решил, что этот concept обрабатывается ЭТИМ прогоном."""
    version = run_passport.registry_version if run_passport is not None else "unversioned"
    key = concept.strip().upper()
    if ad_hoc is not None:
        return MetricDefinition(concept=concept, version=version, source="ad_hoc")
    for row in registry.get("extractable", []):
        if str(row.get("enum", "")).strip().upper() == key:
            return MetricDefinition(
                concept=concept, version=version, source="registry",
                unit_kind=str(row.get("unit_kind") or ""),
                scale_hint=str(row.get("scale_hint") or ""),
                formula_default_abs_behavior=bool(row.get("formula_default_abs_behavior") or False),
                period_type=str(row.get("period_type") or ""),
            )
    # Расчётный, а не извлекаемый концепт — семантика приходит из
    # `calculated_meta` (unit_kind), не из `extractable`.
    meta = (registry.get("calculated_meta") or {}).get(key) or {}
    return MetricDefinition(
        concept=concept, version=version, source="registry",
        unit_kind=str(meta.get("unit_kind") or ""),
    )


def resolve_formula_definition(
    concept: str, registry: dict, run_passport: RunPassport | None, *, ad_hoc: AdHocFormulaSpec | None = None,
) -> FormulaDefinition | None:
    """`FormulaDefinition` для `calc.formula`. `None` — законный исход
    («у этого концепта нет формулы» — извлекаемый-только показатель), не
    исключение: тот же принцип, по которому `_calc_formula_deps` сегодня
    возвращает `()` вместо ошибки.

    Ad-hoc: `dependencies`/`expression` берутся из уже готового
    `AdHocFormulaSpec` — граф-уровня разрешение `root_extra →
    ad_hoc_spec_id → Case` не входит в эту сессию (`Case` — Срез 10, ещё не
    существует; см. план 16a, «Контекст», пункт 1). Зависимость ad-hoc
    формулы принимается, если она есть в реестре ячейки (не домысливаем
    несуществующий концепт как достижимый) ЛИБО описана компонентом формулы
    (Срез 31: модель описала понятие — имя, синонимы, определение, — и его
    извлечёт двухстадийный экстрактор). Вторые перечислены в
    `FormulaDefinition.ad_hoc_dependencies`."""
    version = run_passport.registry_version if run_passport is not None else "unversioned"
    known = _known_concepts(registry)
    if ad_hoc is not None:
        described = {c.concept_key.strip().upper() for c in ad_hoc.components}
        deps: list[str] = []
        ad_hoc_deps: list[str] = []
        for raw in ad_hoc.dependencies:
            key = str(raw).strip().upper()
            if key in known:
                deps.append(key)
            elif key in described:
                deps.append(key)
                ad_hoc_deps.append(key)
        return FormulaDefinition(
            formula_id=ad_hoc.spec_id, version=version, expression=ad_hoc.expression,
            dependencies=deps, ad_hoc_dependencies=ad_hoc_deps, source="ad_hoc",
        )
    key = concept.strip().upper()
    for entry in registry.get("calculated", []):
        if _calc_entry_enum(entry) != key:
            continue
        deps = tuple(d for d in _calc_entry_deps(entry) if d in known)
        meta = (registry.get("calculated_meta") or {}).get(key) or {}
        return FormulaDefinition(
            formula_id=key, version=version, expression=_calc_entry_formula(entry),
            dependencies=list(deps), unit_kind=str(meta.get("unit_kind") or ""),
            annualize=bool(meta.get("annualize") or False),
            hard_min=meta.get("min_value"), hard_max=meta.get("max_value"),
            soft_min=meta.get("soft_min_value"), soft_max=meta.get("soft_max_value"),
            source="registry",
        )
    return None


__all__ = ["resolve_metric_definition", "resolve_formula_definition"]
