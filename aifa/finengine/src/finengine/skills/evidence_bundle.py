"""`EvidenceBundleV2` как адаптер сегодняшнего пути извлечения (Срез 2, B2).

НЕ трогает `two_stage_extractor`/`formula_engine`: читает их уже готовый
результат — конверт `{run, metrics, segments, group}`, который пишет
`aifa-extract/scripts/extract.py` и дополняет `aifa-calc/scripts/calc_run.py`
(строки `is_calculated=True` со вторым каналом происхождения `inputs`,
`skills/ratios.py::compute_ratios`) — и превращает его в контракты A0
(`ScalarFact`/`StructuredFact`/`DerivedFact`), не читая исходный текст отчёта
второй раз и не пересчитывая ничего заново (решение №3).

Один флаг реестра «показатель найден» на строку не значит одно число: строка
несёт `period_values` за НЕСКОЛЬКО периодов (не только `value_current`), и
каждый факт по контракту — «одно число с периодом» (докстринг `ScalarFact`).
Поэтому адаптер разворачивает КАЖДУЮ строку по её периодам, а не берёт только
текущий.

Строка, которую нельзя безопасно превратить в факт (нет числа, нет единицы),
молча ПРОПУСКАЕТСЯ, а не роняет сборку: адаптер — вторичное представление
поверх уже записанного `metrics.json`, и его дефект не имеет права испортить
первичный, сегодняшний пользовательский результат (Definition of Done Среза 2:
«текущие выгрузки работают без изменения пользовательского результата»).

`PeriodAxis` здесь сознательно не строится: чтобы отличить моментный
показатель от интервального, нужны метаданные реестра ячейки (`calculated_meta`/
`_calculated_period_kind_map`), которых нет ни в извлечённых строках, ни у
обоих вызывающих скриптов в равной мере — угадывать point/duration было бы
тем самым домысливанием, которое `id.periods` прямо запрещает. `period_ref`
пока остаётся сырой меткой периода (как в конверте); связать его с
`PeriodAxis` — работа настоящего потребителя (`calc.formula`/`norm.time`,
Срез 6a), которого сегодня в маршруте ещё нет.
"""
from __future__ import annotations

from finengine.contracts import DerivedFact, EvidenceBundleV2, ScalarFact, StructuredFact
from finengine.skills.bulk_extractor.consolidated_group import CONSOLIDATED_GROUP_DIMENSION

ATOM_EXTRACTOR = "two_stage_extractor"
ATOM_FORMULA_ENGINE = "formula_engine"
UNVERSIONED = "unversioned"


def _source_ref(enum: str, page: int | None) -> str:
    return f"{enum}@page:{page}" if page else f"{enum}@extracted"


def _input_enums(raw_inputs: object) -> list[str]:
    """`row["inputs"]` (`skills/ratios.py::compute_ratios`, второй канал
    происхождения) — это список СЛОВАРЕЙ трассировки (`{"enum", "value",
    "substituted_zero", "transform", "period"}`), не голых имён: сам
    вычислитель (`formula_engine._eval_formula_node`) помнит не только КАКИЕ
    входы взял, но и подставил ли ноль вместо пропуска, применил ли
    преобразование. `DerivedFact.inputs` (A0) объявлен как `list[str]` —
    идентификаторы входов, без этой доп. трассировки; ужимаем сюда, не
    выдумывая новое поле контракта в этом срезе.
    """
    out: list[str] = []
    for item in raw_inputs or ():
        if isinstance(item, dict):
            enum = str(item.get("enum") or "").strip()
        else:
            enum = str(item).strip()
        if enum:
            out.append(enum)
    return out


def _facts_from_metric_row(row: dict) -> list[ScalarFact | DerivedFact]:
    """Одна строка движка → факт на КАЖДЫЙ период, где есть число.

    Строка без числа за период (`value is None`) пропускается: пустая ячейка —
    пробел доказательств, а не ноль (решение репозитория «не угадывать»).
    """
    if not isinstance(row, dict) or row.get("dimension"):
        return []
    enum = str(row.get("enum") or "").strip().upper()
    if not enum or not row.get("found"):
        return []
    row_unit = str(row.get("unit") or "").strip()
    is_calculated = bool(row.get("is_calculated"))
    period_values = row.get("period_values") or []
    if not period_values and row.get("value_current") is not None:
        # Строка без разбивки по периодам (сегодня так приходят некоторые свои
        # формулы, `skills/ratios.py::_own_sanity`-строки) — берём то, что есть.
        period_values = [{"period": "", "value": row.get("value_current"),
                          "page": row.get("page")}]
    facts: list[ScalarFact | DerivedFact] = []
    for pv in period_values:
        if not isinstance(pv, dict) or pv.get("value") is None:
            continue
        try:
            value = float(pv["value"])
        except (TypeError, ValueError):
            continue
        unit = str(pv.get("unit") or "").strip() or row_unit
        period_ref = str(pv.get("period") or "") or None
        page = pv.get("page") if pv.get("page") is not None else row.get("page")
        source_ref = _source_ref(enum, page)
        if is_calculated:
            # `DerivedFact.unit` (A0) необязателен — безразмерный коэффициент
            # (например `unit_kind="ratio"` в `skills/ratios.py`) законно не
            # несёт единицы; в отличие от `ScalarFact` ниже, где единица
            # обязательна, здесь отсутствие единицы НЕ повод пропустить факт.
            # `page` контракт `DerivedFact` не несёт вовсе — расчёт не
            # привязан к странице документа, его происхождение — `inputs`.
            facts.append(DerivedFact(
                produced_by_atom=ATOM_FORMULA_ENGINE,
                produced_by_atom_version=UNVERSIONED,
                source_ref=source_ref,
                concept=enum,
                period_ref=period_ref,
                formula_id=enum,
                # Срез 6a: реальную версию формулы несёт `FormulaDefinition`,
                # которого у этого легаси-адаптера (Срез 2, конверт
                # `{run, metrics, ...}`) нет — `calc.formula` (граф, ниже)
                # заполняет `formula_version` из зафиксированной версии
                # реестра напрямую, не через этот путь.
                formula_version=UNVERSIONED,
                inputs=_input_enums(row.get("inputs")),
                value=value,
                unit=unit or None,
            ))
        else:
            if not unit:
                continue
            facts.append(ScalarFact(
                produced_by_atom=ATOM_EXTRACTOR,
                produced_by_atom_version=UNVERSIONED,
                source_ref=source_ref,
                concept=enum,
                period_ref=period_ref or "unknown",
                page=page,
                value=value,
                unit=unit,
            ))
    return facts


def _structured_fact(table_kind: str, rows: list) -> StructuredFact | None:
    clean_rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    if not clean_rows:
        return None
    return StructuredFact(
        produced_by_atom=ATOM_EXTRACTOR,
        produced_by_atom_version=UNVERSIONED,
        source_ref=table_kind,
        table_kind=table_kind,
        rows=clean_rows,
    )


def evidence_bundle_from_envelope(run_id: str, envelope: dict) -> EvidenceBundleV2:
    """Конверт прогона `{run, metrics, segments, group}` → `EvidenceBundleV2`.

    Годится звать и сразу после извлечения (только `ScalarFact`/`StructuredFact`
    — расчёт ещё не выполнялся), и после `calc_run.py` (тот же конверт уже несёт
    строки `is_calculated=True` — на выходе появляются и `DerivedFact`): функция
    просто читает состав `metrics`/`segments`/`group` на момент вызова, а не
    запоминает его между вызовами.
    """
    scalar_facts: list[ScalarFact] = []
    derived_facts: list[DerivedFact] = []
    for row in envelope.get("metrics") or []:
        for fact in _facts_from_metric_row(row):
            (derived_facts if isinstance(fact, DerivedFact) else scalar_facts).append(fact)

    structured_facts: list[StructuredFact] = []
    for table_kind, rows in (
        ("segments", envelope.get("segments")),
        (CONSOLIDATED_GROUP_DIMENSION, envelope.get("group")),
    ):
        fact = _structured_fact(table_kind, rows)
        if fact is not None:
            structured_facts.append(fact)

    return EvidenceBundleV2(
        run_id=run_id,
        scalar_facts=scalar_facts,
        structured_facts=structured_facts,
        derived_facts=derived_facts,
    )


def legacy_rows_from_bundle(
    bundle: EvidenceBundleV2, *, main_period: str = "", prev_period: str = "",
) -> dict:
    """`EvidenceBundleV2` → представление, совместимое по ЧИСЛАМ с `{metrics,
    segments, group}` (A0: «на период миграции `EvidenceBundleV2` должен уметь
    строить совместимое `metrics.json`»).

    Не байт-в-байт повтор исходного конверта — поля показа/диагностики
    (`name_ru`, `formula`-текст, `source_form`, `in_role`, `calc_missing`…)
    факт не несёт по устройству контракта (A0/Фаза 0, не этого среза). Это
    восстановление ЧИСЕЛ по `enum`/периоду — ровно то, что проверяет
    регрессия C7: тот же вход обязан дать те же значения, а не тот же файл
    побайтово. `main_period`/`prev_period` — те же ярлыки, что лежат в
    `run.main_period`/`run.prev_period` исходного конверта: адаптер их не
    выбирает заново (это домен `id.periods`/`norm.time`, не этот срез), только
    смотрит по готовым меткам.
    """
    by_enum: dict[str, dict[str, float]] = {}
    calculated_enums: set[str] = set()
    for fact in (*bundle.scalar_facts, *bundle.derived_facts):
        by_enum.setdefault(fact.concept or "", {})[fact.period_ref or ""] = fact.value
        if isinstance(fact, DerivedFact):
            calculated_enums.add(fact.concept or "")

    unit_by_enum = {f.concept: f.unit for f in bundle.scalar_facts if f.concept}
    unit_by_enum.update({f.concept: f.unit for f in bundle.derived_facts if f.concept})

    metrics = []
    for enum, by_period in sorted(by_enum.items()):
        metrics.append({
            "enum": enum,
            "value_current": by_period.get(main_period),
            "value_prev": by_period.get(prev_period),
            "unit": unit_by_enum.get(enum),
            "found": True,
            "is_calculated": enum in calculated_enums,
        })

    segments, group = [], []
    for fact in bundle.structured_facts:
        target = group if fact.table_kind == CONSOLIDATED_GROUP_DIMENSION else segments
        target.extend(fact.rows)

    return {"metrics": metrics, "segments": segments, "group": group}
