"""`calc.formula` — карточка 16a: детерминированный расчёт по проверенным
`MetricEvidence`; planning-выход — `CalculatedMetric`, evidence payload —
`DerivedFact`. Эволюция `calc.formula_engine` (Срез 3, `graph_atoms.py`), не
второй атом рядом со старым — переименование того же узла с расширенным
контрактом (см. отчёт по Срезу 6a: канонический атом v9 называется
`calc.formula`, не `calc.formula_engine`).

**Не переписывает `formula_engine`.** Числовое ядро (`skills.ratios.
compute_ratios` → `bulk_extractor.formula_engine`) вызывается тем же образом,
что и раньше, через тот же адаптер (`legacy_rows_from_bundle`/
`evidence_bundle_from_envelope`, Срез 2). Новое здесь — (1) зависимости
формулы раскрываются через типизированный `FormulaDefinition` (`skills.atoms.
definitions.resolve_formula_definition`), не через чтение сырого кортежа
реестра внутри `dynamic_requires`; (2) `DerivedFact` несёт версию/текст
формулы и evidence_id реальных зависимостей — dependency graph, а не только
число; (3) брак формулы, не перехваченный внутренним расчётом одного
показателя (запрещённый AST ad-hoc формулы — whitelist уже живёт в
`formula_engine._parse_formula_expr`, не переизобретается), превращается в
явный `VerificationFinding`, а не необработанное исключение.

**Деление на ноль.** `compute_ratios`/`formula_engine` уже перехватывают его
ВНУТРИ расчёта ОДНОГО показателя (широкие `except Exception` вокруг
`_eval_formula_node`, чтобы одна плохая формула не роняла весь пакет
показателей) и отдают для него пустой результат — тот же класс исхода, что
«недостающая зависимость» (см. `test_calc_formula.py::
test_division_by_zero_never_publishes_a_calculated_fact`). Наружу к этому
узлу исключение поэтому не долетает; `try/except` ниже — вторая линия
защиты для ошибок формулы, которые НЕ перехвачены на том внутреннем уровне
(например, брак разбора ad-hoc-выражения ДО попытки его вычислить).
"""
from __future__ import annotations

import ast

from finengine.contracts import (
    AdHocFormulaSpec,
    CalculatedMetric,
    DerivedFact,
    EvidenceBundleV2,
    ExtractedMetric,
    RunPassport,
    ScalarFact,
    UserProvidedMetric,
    VerificationFinding,
)
from finengine.skills.atoms.ad_hoc_concepts import EXTRA_KEY, dump_specs, load_specs
from finengine.skills.atoms.definitions import resolve_formula_definition
from finengine.skills.evidence_bundle import evidence_bundle_from_envelope, legacy_rows_from_bundle
from finengine.skills.metric_schema import registry_for_passport
from finengine.skills.periods import normalize_period_label, period_kind_for_enum
from finengine.skills.ratios import compute_ratios
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec
from finengine.skills.bulk_extractor.formula_engine import _normalise_formula_expr
from finengine.skills.bulk_extractor.period_selection import _period_boundary_keys, _point_period_key

ATOM_VERSION = "2.2.0"  # Preserve calculated dependencies as DerivedFact inputs.


def _averaged_dependencies(expression: str, dependencies: list[str]) -> set[str]:
    tree = ast.parse(_normalise_formula_expr(expression), mode="eval")
    return {name.id for call in ast.walk(tree)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id.upper() == "AVG"
            for arg in call.args for name in ast.walk(arg)
            if isinstance(name, ast.Name) and name.id in dependencies}


def _average_boundaries(period: str | None):
    return _period_boundary_keys(normalize_period_label(period, value_kind="duration")) if period else None


def _ad_hoc_spec_from_extra(node_args: NodeArgs) -> AdHocFormulaSpec | None:
    """R4.5/Срез 6a: ad-hoc формула БЕЗ `Case` (Срез 10, ещё не существует)
    доезжает до этого узла только тем же способом, каким `role`/`ad_hoc_spec_id`
    уже доезжают до других узлов сегодня — через `NodeArgs.extra` (см.
    `TargetFactory.root_from_goal`/`_compose_form_scope`). Полное graph-уровня
    разрешение `root_extra → Case → сюда` — вне периметра этой сессии (план
    16a, «Контекст», пункт 1); эта функция — точка расширения, unit-тестируемая
    напрямую, без графового прогона."""
    expr = node_args.extra.get("ad_hoc_formula_expression")
    if not expr:
        return None
    named_term = node_args.extra.get("ad_hoc_formula_term")
    if named_term and node_args.concept and node_args.concept != named_term:
        # `extra` корня наследуют ВСЕ дочерние узлы. Дочерний `calc.formula` с другим
        # концептом — это расчётный показатель реестра (вход ad-hoc формулы), а не сама
        # формула: иначе он прочёл бы её выражение вместо своей записи в реестре и
        # потребовал бы самого себя («цикл зависимостей», M2).
        return None
    deps_raw = node_args.extra.get("ad_hoc_formula_dependencies", "")
    deps = [d.strip() for d in deps_raw.split(",") if d.strip()]
    term = named_term or node_args.concept or "ad_hoc"
    return AdHocFormulaSpec(
        term=term, expression=expr, dependencies=deps,
        assumptions=node_args.extra.get("ad_hoc_formula_assumptions", ""),
        unit=node_args.extra.get("ad_hoc_formula_unit", ""),
        # Срез 31: компоненты, описанные моделью, едут в `extra` тем же каналом.
        components=list(load_specs(node_args.extra).values()),
    )


def calc_formula_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    """A1.6: зависимости формулы раскрываются здесь, ДО запуска исполнителя
    — Planner материализует `FormulaDefinition` и заводит Targets её
    `dependencies` как обычные требуемые входы графа.

    Срез 31: зависимость, описанная моделью и отсутствующая в реестре ячейки
    (`ad_hoc_dependencies`), извлекается НАПРЯМУЮ как `ExtractedMetric` —
    объединение `MetricEvidence` матчит только члены реестра (см.
    `AtomRegistry.producers_of`), catch-all для ad-hoc понятий есть лишь у
    прямого `ExtractedMetric`."""
    if run_passport is None or not node_args.concept:
        return ()
    registry = registry_for_passport(run_passport)
    ad_hoc = _ad_hoc_spec_from_extra(node_args)
    formula_def = resolve_formula_definition(node_args.concept, registry, run_passport, ad_hoc=ad_hoc)
    if formula_def is None:
        return ()
    ad_hoc_deps = set(formula_def.ad_hoc_dependencies)
    averaged = _averaged_dependencies(formula_def.expression, formula_def.dependencies)
    boundaries = _average_boundaries(node_args.period_ref)
    requires = []
    for dep in formula_def.dependencies:
        kind = "ExtractedMetric" if dep in ad_hoc_deps else "MetricEvidence"
        if dep in averaged and boundaries:
            requires.extend(InputSpec(input_type=kind, concept=dep, period_ref=f"{day:02}.{month:02}.{year}")
                            for year, month, day in boundaries)
        else:
            requires.append(InputSpec(input_type=kind, concept=dep, period_ref="" if dep in averaged else None))
    return tuple(requires)


def calc_formula_requirement_extra(node_args: NodeArgs, requirement: InputSpec) -> dict[str, str]:
    """Give each formula input only the metadata that changes its extraction.

    Formula expression/spec id/unit belong to the ``calc.formula`` root and
    must not leak into registered child metrics: in a compound plan that made
    ``locate(NET_INCOME)`` differ from the same standalone extraction despite
    an identical NodeKey. A genuinely ad-hoc dependency still needs its own
    aliases/definition, so it receives one filtered concept spec.
    """
    if requirement.input_type != "ExtractedMetric" or not requirement.concept:
        return {}
    spec = load_specs(node_args.extra).get(requirement.concept.strip().upper())
    return {EXTRA_KEY: dump_specs([spec])} if spec is not None else {}


def _scalar_input(evidence: ExtractedMetric | UserProvidedMetric) -> ScalarFact:
    if isinstance(evidence, ExtractedMetric):
        return evidence.scalar_fact
    h = evidence.human_override
    if h.numeric_value is None or not (h.unit or '').strip():
        raise ValueError('calc.formula: active user override must be numeric and have a unit')
    # Compatibility adapter for the unchanged formula engine. This is NOT a
    # persisted document fact: its evidence_id is the immutable HumanOverride
    # id, so DerivedFact.inputs keeps the real audit dependency.
    return ScalarFact(
        evidence_id=h.evidence_id, produced_by_atom='read.override',
        produced_by_atom_version=h.produced_by_atom_version, source_ref=h.source_ref,
        document_version=h.document_version, concept=evidence.concept,
        period_ref=evidence.period_ref, basis=h.basis, value=h.numeric_value, unit=h.unit,
        supersedes=h.supersedes, created_at=h.created_at,
    )


def _align_to_requested_period(
    requested: str, facts: list[ScalarFact | DerivedFact],
) -> tuple[str, list[ScalarFact | DerivedFact]]:
    """`(main_period, facts)` для legacy-конверта: зависимости, относящиеся к
    ЗАПРОШЕННОМУ периоду, лежат под одной меткой.

    GoalIR несёт то, что сказал пользователь («2024»), а `read.value` отдаёт
    метки документа: поток — «FY 2024», остаток — «31.12.2024».
    `legacy_rows_from_bundle` сверяет `main_period` с метками фактов ТОЧНО, и без
    выравнивания оба операнда извлекались верно, а расчёт молча возвращал `[]`
    (real-model smoke, Срез 30: EBITDA и фондоотдача). Нужная метка каждой
    зависимости определяется её собственным видом (`period_kind_for_enum`):
    для остатка «2024» — это 31.12.2024, для потока — FY 2024. Совпавшие факты
    приводятся к одной метке в ЛОКАЛЬНОЙ копии (evidence_id не меняются, это не
    второй источник числа); факты другого периода не подмешиваются — тогда всё
    остаётся как раньше и результат пуст."""
    if not requested:
        return requested, facts
    matching = [
        f for f in facts
        if normalize_period_label(f.period_ref)
        == normalize_period_label(requested, value_kind=period_kind_for_enum(f.concept))
    ]
    if not matching:
        return requested, facts
    main_label = next(
        (f.period_ref for f in matching if period_kind_for_enum(f.concept) == "duration"),
        matching[0].period_ref,
    )
    ids = {id(f) for f in matching}
    return main_label, [f.model_copy(update={"period_ref": main_label}) if id(f) in ids else f for f in facts]


def _period_slices(facts: list[ScalarFact | DerivedFact]) -> list[tuple[str, list[ScalarFact | DerivedFact]]]:
    """Срезы входных фактов по периодам: `[(главная метка, факты с выровненными
    метками), ...]` — по одному на каждый различный период.

    Поток («FY 2024») и остаток («31.12.2024») одного периода — ОДИН срез:
    `_align_to_requested_period` приводит их к одной метке, а повтор срезов с
    одинаковой нормализованной главной меткой отбрасывается. Нет фактов —
    один пустой срез, чтобы поведение «нечего считать → пусто» не менялось."""
    slices: list[tuple[str, list[ScalarFact | DerivedFact]]] = []
    seen: set[str] = set()
    for label in dict.fromkeys(f.period_ref for f in facts if f.period_ref):
        main_label, aligned = _align_to_requested_period(label, facts)
        key = normalize_period_label(main_label)
        if key in seen:
            continue
        seen.add(key)
        slices.append((main_label, aligned))
    return slices or [("", facts)]


def calc_formula_executor(args: NodeArgs, ctx) -> list:
    passport = ctx.run_passport
    if passport is None:
        raise ValueError("calc.formula: RunPassport не зафиксирован — узел не должен был стать ready")
    registry = registry_for_passport(passport)
    ad_hoc = _ad_hoc_spec_from_extra(args)
    formula_def = resolve_formula_definition(args.concept or "", registry, passport, ad_hoc=ad_hoc)
    if formula_def is None:
        # Показателя формулы не существует (извлекаемый-только концепт) —
        # штатный пустой результат, тот же класс исхода, что «показатель не
        # нашёлся в отчёте» у read.value, не техническая ошибка.
        return []

    deps = [
        e for e in ctx.dependencies()
        if isinstance(e, (ExtractedMetric, CalculatedMetric, UserProvidedMetric)) and e.concept in formula_def.dependencies
    ]
    scalar_facts = [_scalar_input(e) for e in deps if isinstance(e, (ExtractedMetric, UserProvidedMetric))]
    derived_facts = [e.derived_fact for e in deps if isinstance(e, CalculatedMetric)]
    input_facts = [*scalar_facts, *derived_facts]
    requested = args.period_ref or ""
    # Срез 31 (П6): период не назван — считаем по КАЖДОМУ периоду, найденному во
    # входных фактах (все доступные даты). Назван — один период, как раньше.
    period_slices = (
        [_align_to_requested_period(requested, input_facts)] if requested
        else _period_slices(input_facts)
    )
    extra_formulas = None
    if formula_def.source == "ad_hoc":
        # `"enum"` здесь ДОЛЖЕН совпадать с `args.concept` — это то самое имя,
        # по которому `only=[args.concept]` ниже фильтрует `compute_ratios`, и
        # то самое имя, по которому `raw_facts` ищет результат в `derived_
        # facts` после. `formula_def.formula_id` — внутренний `ad_hoc.spec_id`
        # (UUID), под этим именем `compute_ratios` формулу искать не станет —
        # раньше это никогда не исполнялось с реальными expression/
        # dependencies, поэтому расхождение оставалось незамеченным (Срез 27).
        extra_formulas = [{
            "enum": args.concept, "name_ru": ad_hoc.term if ad_hoc else formula_def.formula_id,
            "formula": formula_def.expression, "deps": formula_def.dependencies,
        }]

    # `_facts_from_metric_row` (evidence_bundle.py) uppercases every `enum` on
    # the way in — a convention that matches canonical registry enums, but
    # shouts back an ad-hoc term the user typed in lowercase. Compare
    # case-insensitively; `args.concept` (original casing) still wins below.
    concept_key = (args.concept or "").strip().upper()
    results: list[CalculatedMetric] = []
    failures: list[VerificationFinding] = []
    for main_period, _ in period_slices:
        envelope = legacy_rows_from_bundle(
            EvidenceBundleV2(run_id=ctx.run_id, scalar_facts=scalar_facts, derived_facts=derived_facts),
            main_period=main_period,
        )
        try:
            computed = compute_ratios(
                envelope["metrics"], passport.classification.profile_id, only=[args.concept],
                extra=extra_formulas, registry_override=registry,
                period_current=main_period,
            )
        except (ValueError, ZeroDivisionError) as exc:
            # Вторая линия защиты (см. докстринг модуля) — деление на ноль внутри
            # РАСЧЁТА обычно перехвачено самим `compute_ratios` и не долетает
            # сюда; здесь ловятся ошибки уровнем выше (например, `_FormulaError`
            # — подкласс `ValueError` — при разборе запрещённой конструкции
            # ad-hoc AST, whitelist `formula_engine._parse_formula_expr`) — явный
            # отказ ДО публикации результата, не необработанное исключение.
            failures.append(VerificationFinding(
                produced_by_atom="calc.formula", produced_by_atom_version=ATOM_VERSION,
                source_ref=formula_def.formula_id, concept=args.concept,
                period_ref=main_period or args.period_ref,
                check_kind="bounds_hard", status="fail",
                detail=f"формула {formula_def.formula_id!r} ({formula_def.expression!r}) не вычислена: {exc}",
            ))
            continue

        result_bundle = evidence_bundle_from_envelope(ctx.run_id, {"metrics": computed})
        raw_facts = [f for f in result_bundle.derived_facts
                     if (f.concept or "").strip().upper() == concept_key
                     and normalize_period_label(f.period_ref) == normalize_period_label(main_period)]
        if not raw_facts:
            # Недостающая зависимость — штатный пустой результат (0 доказательств),
            # НЕ заменяется нулём в значении (карточка: «отсутствующий вход не
            # заменяется нулём и не даёт рассчитанный факт»).
            averaged = _averaged_dependencies(formula_def.expression, formula_def.dependencies)
            missing = [f"{dep} за {normalize_period_label(main_period, value_kind=period_kind_for_enum(dep))}"
                       for dep in formula_def.dependencies if dep not in averaged
                       and not any(f.concept == dep and normalize_period_label(f.period_ref)
                                   == normalize_period_label(main_period, value_kind=period_kind_for_enum(dep))
                                   for f in input_facts)]
            boundaries = _average_boundaries(main_period)
            if boundaries:
                for dep in sorted(averaged):
                    for boundary in boundaries:
                        if not any(f.concept == dep and _point_period_key(f.period_ref) == boundary for f in input_facts):
                            year, month, day = boundary
                            missing.append(f"{dep} на {day:02}.{month:02}.{year}")
            detail = (f"Не рассчитан {args.concept} за {main_period}: нет входных значений: {', '.join(missing)}"
                      if missing else f"Не рассчитан {args.concept} за {main_period}: вычислитель не вернул результат; "
                      "проверьте периоды входов, единицы и допустимость формулы (включая деление на ноль).")
            failures.append(VerificationFinding(
                produced_by_atom="calc.formula", produced_by_atom_version=ATOM_VERSION,
                source_ref=formula_def.formula_id, concept=args.concept, period_ref=main_period or None,
                check_kind="bounds_hard", status="fail", detail=detail,
            ))
            continue

        averaged = _averaged_dependencies(formula_def.expression, formula_def.dependencies)
        boundaries = _average_boundaries(main_period)
        dep_evidence_ids = [f.evidence_id for f in input_facts
                            if (f.concept in averaged and boundaries and _point_period_key(f.period_ref) in boundaries)
                            or (f.concept not in averaged and normalize_period_label(f.period_ref)
                                == normalize_period_label(main_period, value_kind=period_kind_for_enum(f.concept)))]
        for f in raw_facts:
            enriched = DerivedFact(
                produced_by_atom="calc.formula", produced_by_atom_version=ATOM_VERSION,
                source_ref=f.source_ref, document_version=f.document_version, concept=args.concept or f.concept,
                period_ref=f.period_ref or main_period or None, basis=f.basis, formula_id=formula_def.formula_id,
                formula_version=formula_def.version, expression=formula_def.expression,
                inputs=dep_evidence_ids or f.inputs, value=f.value,
                # Dimensionless ad-hoc ratios make the legacy formula engine
                # return an empty unit.  The intent contract already carries
                # the user's/model's declared unit; preserve it at publication
                # instead of silently dropping it.
                unit=f.unit or (ad_hoc.unit if ad_hoc else None),
            )
            results.append(CalculatedMetric(
                derived_fact=enriched, concept=enriched.concept or args.concept,
                period_ref=enriched.period_ref or main_period or "unknown", definition_origin=formula_def.source,
            ))
    # With no requested period, missing comparative-only slices must not
    # invalidate the periods that actually have enough inputs to calculate.
    return results if results and not requested else [*results, *failures]


__all__ = [
    "calc_formula_requires", "calc_formula_requirement_extra",
    "calc_formula_executor", "ATOM_VERSION",
]
