"""Статус каждого показателя в состоянии цикла (ADR-0083): найден ли движком, посчитан ли и чего не хватило.

Слова строгие: корень выбирает способ переделки шага по ним, а не по догадке из текста отказа.
«НЕ НАЙДЕН» и «НЕ РАССЧИТАН» пишутся только там, где движок пробовал; непробованное — «не пробовали».
Значения берутся из таблицы артефактов (`ExtractedMetric`, `CalculatedMetric`), состав формул — из реестра.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

EXTRACT = "extract"
CALC = "calc"

_EXTRACT_KINDS = ("extract_registered_metric", "extract_ad_hoc_metric")
_CALC_KINDS = ("calculate_registered_metric", "calculate_ad_hoc_metric")
_VALUE_TYPES = {"ExtractedMetric": "extracted", "CalculatedMetric": "calculated"}

LEGEND = (
    "Слова строгие. «найден» / «рассчитан» — значение есть в таблице артефактов. «НЕ НАЙДЕН» — движок пробовал "
    "извлечь, значения нет. «НЕ РАССЧИТАН: нет значений: …» — движок пробовал считать, перечисленных значений "
    "не хватило. «не пробовали» — движок этого ещё не делал."
)


def produced_from_rows(rows: Iterable[Mapping]) -> dict[str, dict[str, set[str]]]:
    """Какие периоды показателя получены: отдельно извлечением и отдельно расчётом."""
    produced: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        for payload in row["payloads"]:
            kind = _VALUE_TYPES.get(getattr(payload, "output_type", None) or "")
            concept = str(getattr(payload, "concept", "") or "").strip().upper()
            if kind is None or not concept:
                continue
            slot = produced.setdefault(concept, {"extracted": set(), "calculated": set()})
            slot[kind].add(str(getattr(payload, "period_ref", "") or "—"))
    return produced


def _calculated(registry: Mapping) -> dict[str, tuple]:
    return {str(entry[0]).strip().upper(): entry for entry in registry["calculated"]}


def _extractable(registry: Mapping) -> dict[str, Mapping]:
    return {str(metric["enum"]).strip().upper(): metric for metric in registry["extractable"]}


def _components(entry: tuple) -> list[str]:
    return [str(dep).strip().upper() for dep in (entry[3] or []) if str(dep).strip()]


def _expand(registry: Mapping, enums: Iterable[str]) -> list[str]:
    """Показатели и, рекурсивно, компоненты их формул — в порядке появления, без повторов."""
    calc = _calculated(registry)
    order: list[str] = []

    def visit(enum: str) -> None:
        if enum in order:
            return
        order.append(enum)
        if enum in calc:
            for dep in _components(calc[enum]):
                visit(dep)

    for enum in enums:
        visit(enum)
    return order


def requested_enums(clauses: Iterable) -> list[str]:
    """Показатели частей цели, которые движок получает: извлечение и расчёт."""
    found: list[str] = []
    for clause in clauses:
        if clause.intent_kind in (*_EXTRACT_KINDS, *_CALC_KINDS):
            found += [str(target.concept).strip().upper() for target in clause.targets if target.concept]
    return found


def _mark_calculation(attempts: dict[str, set[str]], registry: Mapping, enum: str) -> None:
    """Расчёт пробует и компоненты формулы: считанные — расчётом, остальные — извлечением."""
    calc = _calculated(registry)
    for item in _expand(registry, [enum]):
        attempts.setdefault(item, set()).add(CALC if item in calc else EXTRACT)


def record_attempts(attempts: dict[str, set[str]], registry: Mapping, clause) -> None:
    """Движок пробовал исполнить часть цели по ссылке."""
    for enum in requested_enums([clause]):
        if clause.intent_kind in _EXTRACT_KINDS:
            attempts.setdefault(enum, set()).add(EXTRACT)
        else:
            _mark_calculation(attempts, registry, enum)


_STEP_EXTRACTION_TYPES = ("ExtractedMetric", "CandidateLocator")


def record_step_attempts(attempts: dict[str, set[str]], registry: Mapping, steps: Iterable) -> None:
    """Движок исполнил шаги атомов: поиск места и чтение значения — попытка извлечь, расчёт — попытка посчитать.

    Записывается и когда значения нет: шаг пробовали, и «не пробовали» в состоянии было бы неправдой."""
    for step in steps:
        enum = str(getattr(step, "concept", "") or "").strip().upper()
        if not enum:
            continue
        if step.output_type in _STEP_EXTRACTION_TYPES:
            attempts.setdefault(enum, set()).add(EXTRACT)
        elif step.output_type == "CalculatedMetric":
            _mark_calculation(attempts, registry, enum)


def scope_enums(clauses: Iterable, attempts: Mapping[str, set[str]]) -> list[str]:
    """Что показывает раздел: показатели частей цели, затем всё, что движок пробовал шагами."""
    return list(dict.fromkeys([*requested_enums(clauses), *attempts]))


def _has_value(produced: Mapping, enum: str) -> bool:
    slot = produced.get(enum)
    return bool(slot and (slot["extracted"] or slot["calculated"]))


def _missing_values(registry: Mapping, produced: Mapping, enum: str, seen: frozenset[str] = frozenset()) -> list[str]:
    """Значения, которых не хватает формуле: названо отсутствующее, а не промежуточный итог."""
    calc = _calculated(registry)
    entry = calc.get(enum)
    if entry is None or enum in seen:
        return []
    missing: list[str] = []
    for dep in _components(entry):
        if _has_value(produced, dep):
            continue
        deeper = _missing_values(registry, produced, dep, seen | {enum}) if dep in calc else []
        missing += deeper or [dep]
    return list(dict.fromkeys(missing))


def _periods(periods: set[str]) -> str:
    return ", ".join(sorted(periods))


def metric_status_lines(
    registry: Mapping, scope: Sequence[str], produced: Mapping, attempts: Mapping[str, set[str]],
) -> list[str]:
    calc = _calculated(registry)
    extractable = _extractable(registry)
    lines = []
    for enum in _expand(registry, scope):
        in_registry = enum in extractable or enum in calc
        name = (extractable.get(enum) or {}).get("name_ru") or (calc[enum][1] if enum in calc else "")
        title = f"{enum} ({name})" if in_registry else f"{enum} (вне реестра)"
        slot = produced.get(enum) or {"extracted": set(), "calculated": set()}
        tried = attempts.get(enum, set())
        if slot["extracted"]:
            extraction = f"найден ({_periods(slot['extracted'])})"
        elif in_registry and enum not in extractable:
            extraction = "не извлекается (нет среди «Извлекаемых»)"
        else:
            extraction = "НЕ НАЙДЕН" if EXTRACT in tried else "не пробовали"
        if slot["calculated"]:
            calculation = f"рассчитан ({_periods(slot['calculated'])})"
        elif in_registry and enum not in calc:
            calculation = "формулы реестра нет"
        elif CALC in tried:
            missing = _missing_values(registry, produced, enum)
            calculation = (
                f"НЕ РАССЧИТАН: нет значений: {', '.join(missing)}" if missing
                else "НЕ РАССЧИТАН: значения компонентов есть, расчёт не получен"
            )
        else:
            calculation = "не пробовали"
        lines.append(f"- {title}: извлечение — {extraction}; расчёт — {calculation}")
    return lines
