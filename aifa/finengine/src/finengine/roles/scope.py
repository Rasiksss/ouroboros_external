from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from finengine.roles.access import (
    ALL_EXTRACTION_ROLE,
    EXTRACTION_ROLES,
    OTHER_ROLE,
    ROLE_LABELS,
    normalize_extraction_role,
)
# ⚠️ ЛЕГАСИ-РЕЕСТР (`data/metrics.json`) БОЛЬШЕ НЕ ЧИТАЕТСЯ ОТСЮДА (21.08.2026).
# Он был запасным, когда ячейка неизвестна, — и молча расходился с ячейками:
# 12 формул у банка и 2 у нефинансовой давали ДРУГОЕ число под тем же именем, а
# после перевода доходностей в проценты — в сто раз меньшее. Замерено: легаси
# оказался строгим ПОДМНОЖЕСТВОМ ячейки (в нём нет НИ ОДНОГО показателя, которого
# нет в ячейке), то есть замена ничего не отнимает и добавляет 2 извлекаемых и
# 5 расчётных. Запасной путь теперь ведёт в ячейку по умолчанию, а не во вторую
# копию знания.
_DEFAULT_CELL = {"bank": "ifrs_bank_ru", "non_financial": "ifrs_nf_ru"}


LAYOUTS_PATH = Path(__file__).with_name("layouts.json")

_PSEUDO_DEPS: dict[str, set[str]] = {
    "SALES_REVENUE_LTM": {"SALES_REVENUE"},
    "EBITDA_LTM": {"EBITDA"},
    "NET_INCOME_LTM": {"NET_INCOME"},
}


@dataclass(frozen=True)
class RolePlan:
    role: str
    role_label: str
    company_type: str
    format: str
    config: dict[str, Any] | None
    extraction_enums: set[str] | None
    output_enums: set[str] | None

    @property
    def is_all(self) -> bool:
        return self.role == ALL_EXTRACTION_ROLE


@dataclass(frozen=True)
class RoleSegmentPolicy:
    include_segments: bool
    segment_mode: str = "core"
    segment_context_mode: str = "full"


@lru_cache(maxsize=1)
def layouts() -> dict[str, Any]:
    return json.loads(LAYOUTS_PATH.read_text(encoding="utf-8-sig"))


def canonical_company_type(company_type: str | None) -> str:
    return "bank" if (company_type or "").strip().lower() == "bank" else "non_financial"


def _registry(company_type: str) -> tuple[set[str], dict[str, tuple[str, str, str, list[str]]]]:
    """Состав, когда ЯЧЕЙКА НЕИЗВЕСТНА: ячейка по умолчанию для типа компании.

    Раньше здесь читался `data/metrics.json` — вторая копия того же знания.
    Копия расходилась, и разойтись ей было позволено молча: имя показателя то
    же, формула другая.
    """
    return _registry_for_profile_id(_DEFAULT_CELL[canonical_company_type(company_type)])


def _registry_for_profile_id(
    profile_id: str,
) -> tuple[set[str], dict[str, tuple[str, str, str, list[str]]]]:
    """Same as `_registry` but from a CELL's own per-cell registry, so the role
    plan respects metrics a cell added/dropped (not just the company_type base)."""
    from finengine.skills.metric_schema import registry_for_profile

    reg = registry_for_profile(profile_id)
    extractable_enums = {str(m["enum"]).strip().upper() for m in reg["extractable"]}
    calc_by_enum = {str(c[0]).strip().upper(): c for c in reg["calculated"]}
    return extractable_enums | set(calc_by_enum), calc_by_enum


def _row_enum(row: dict[str, Any]) -> str | None:
    enum = str(row.get("enum") or "").strip().upper()
    return enum or None


def _layout_output_enums(config: dict[str, Any]) -> set[str]:
    fmt = str(config.get("format") or "")
    out: set[str] = set()
    for row in config.get("rows") or []:
        enum = _row_enum(row)
        if not enum:
            continue
        if fmt in {"dgr", "mglob"} and not row.get("is_available"):
            continue
        out.add(enum)
    return out


def _expand_deps(
    enum: str,
    *,
    available_enums: set[str],
    calc_by_enum: dict[str, tuple[str, str, str, list[str]]],
    seen: set[str] | None = None,
) -> set[str]:
    enum = enum.strip().upper()
    seen = seen or set()
    if enum in seen:
        return set()
    seen.add(enum)

    deps = {d.strip().upper() for d in _PSEUDO_DEPS.get(enum, set()) if d.strip()}
    calc = calc_by_enum.get(enum)
    if calc:
        deps.update(str(d).strip().upper() for d in (calc[3] or []) if str(d).strip())

    out: set[str] = set()
    for dep in deps:
        if dep not in available_enums:
            continue
        out.add(dep)
        out.update(_expand_deps(dep, available_enums=available_enums, calc_by_enum=calc_by_enum, seen=seen))
    return out


def get_role_config(role: str | None, company_type: str | None) -> dict[str, Any] | None:
    normalized = normalize_extraction_role(role)
    if normalized == ALL_EXTRACTION_ROLE:
        return None
    role_cfg = (layouts().get("roles") or {}).get(normalized) or {}
    formats = role_cfg.get("formats") or {}
    return formats.get(canonical_company_type(company_type))


def role_supported_for_company_type(role: str | None, company_type: str | None) -> bool:
    normalized = normalize_extraction_role(role)
    if normalized in {ALL_EXTRACTION_ROLE, OTHER_ROLE}:
        return True
    if normalized not in EXTRACTION_ROLES:
        return False
    cfg = get_role_config(normalized, company_type)
    return bool(cfg and _layout_output_enums(cfg))


def role_should_include_segments(role: str | None, company_type: str | None) -> bool:
    return role_segment_policy(role, company_type).include_segments


def role_segment_policy(role: str | None, company_type: str | None) -> RoleSegmentPolicy:
    """Segment extraction policy for role-driven product runs.

    Role-based extraction intentionally asks only for core business
    breakdowns and sends the full document to the segment stage. Extended
    cross-breakdowns remain available in benchmark/debug flows, but are not
    part of the role product contract.
    """

    normalized = normalize_extraction_role(role)
    company = canonical_company_type(company_type)
    include = normalized == OTHER_ROLE or (
        company == "non_financial" and normalized in {"die", "cib"}
    )
    return RoleSegmentPolicy(
        include_segments=include,
        segment_mode="core",
        segment_context_mode="full",
    )


def role_should_include_consolidated_group(role: str | None, company_type: str | None) -> bool:
    """Only the generic "other" role receives group composition rows."""
    return normalize_extraction_role(role) == OTHER_ROLE


def build_role_plan(
    role: str | None, company_type: str | None, profile_id: str | None = None
) -> RolePlan:
    normalized = normalize_extraction_role(role)
    company = canonical_company_type(company_type)
    if normalized in {ALL_EXTRACTION_ROLE, OTHER_ROLE}:
        return RolePlan(
            role=normalized,
            role_label=ROLE_LABELS[normalized],
            company_type=company,
            format="generic",
            config=None,
            extraction_enums=None,
            output_enums=None,
        )

    cfg = get_role_config(normalized, company)
    if not cfg:
        return RolePlan(
            role=normalized,
            role_label=ROLE_LABELS.get(normalized, normalized),
            company_type=company,
            format="unsupported",
            config=None,
            extraction_enums=set(),
            output_enums=set(),
        )

    available_enums, calc_by_enum = (
        _registry_for_profile_id(profile_id) if profile_id else _registry(company)
    )
    layout_enums = _layout_output_enums(cfg)
    output_enums = {e for e in layout_enums if e in available_enums}

    extraction_enums = set(output_enums)
    for enum in layout_enums:
        if enum in available_enums:
            extraction_enums.update(
                _expand_deps(enum, available_enums=available_enums, calc_by_enum=calc_by_enum)
            )
        elif enum in _PSEUDO_DEPS:
            for dep in _PSEUDO_DEPS[enum]:
                if dep in available_enums:
                    extraction_enums.add(dep)
                    extraction_enums.update(
                        _expand_deps(dep, available_enums=available_enums, calc_by_enum=calc_by_enum)
                    )

    return RolePlan(
        role=normalized,
        role_label=ROLE_LABELS.get(normalized, normalized),
        company_type=company,
        format=str(cfg.get("format") or ""),
        config=cfg,
        extraction_enums=extraction_enums,
        output_enums=output_enums,
    )


def role_not_applied(plan: "RolePlan") -> bool:
    """Роль выбрана, но раскладки для этой отчётности нет — сужения не будет.

    Пара вроде «ДГР × нефинансовая» или «Риски × банк» проваливалась в полный
    набор ячейки МОЛЧА: человек выбирал узкую роль, получал 119 показателей и не
    мог понять, сработал его выбор или нет. Данные при этом верные, поэтому
    отказывать не за что — но факт «роль не применилась» обязан быть сказан.
    Живёт здесь, рядом с решением о составе: иначе бэкенд и станция начнут
    отвечать на один и тот же вопрос по-разному.
    """
    return plan.role not in (ALL_EXTRACTION_ROLE, OTHER_ROLE) and not plan.extraction_enums


def role_notice(plan: "RolePlan", company_type: str, profile_id: str) -> str:
    """Та же новость СЛОВАМИ — фраза, которую увидит человек.

    Живёт рядом с самим условием (`role_not_applied`), а не у вызывающего:
    произносить её обязаны ОБА пути — и тул, и слушатель прогона станции. Пока
    текст был f-строкой внутри хендлера тула, второй путь либо промолчал бы,
    либо завёл свою редакцию — и пользователь получал бы разные объяснения
    одного и того же в зависимости от того, кто поднял комнату.

    Пустая строка при применившейся роли: «сказать нечего» — это тоже ответ, и
    проверять условие второй раз у вызывающего незачем.
    """
    if not role_not_applied(plan):
        return ""
    kind = "банк" if company_type == "bank" else "нефинансовая компания"
    return (
        f"Роль «{plan.role_label}» для этой отчётности ({kind}, ячейка "
        f"{profile_id}) не заведена — извлекаю ПОЛНЫЙ набор ячейки, без "
        f"сужения под роль."
    )


def _narrow_to_requested(
    plan: RolePlan, requested: "Iterable[str]", *, profile_id: str | None,
    own_deps: "Iterable[str] | None" = None,
) -> RolePlan:
    """План под ЯВНО названный список показателей (Ш13).

    Отдельной функцией, а не двумя строками внутри `build_engine_scope`,
    потому что решение здесь ровно одно и оно неочевидно: ЧИТАЕМ шире, чем
    ПОКАЗЫВАЕМ. Запрошенный коэффициент без своих слагаемых не посчитается, а
    слагаемые в таблице человек не просил и видеть их там не должен.

    Имена, которых в ячейке нет, отбрасываются молча ЗДЕСЬ и говорятся вслух
    ТАМ, где есть кому сказать (тул сверяет свой список с итоговым и пишет об
    отброшенных в ответ модели). Сюда сообщение положить некуда: функцию зовут
    и бэкенд, и станция.

    `own_deps` — ВХОДЫ СВОИХ ФОРМУЛ (`--extra-calculated`). Читаются наравне с
    входами реестровых, но в показ НЕ идут: человек просил коэффициент, а не
    его слагаемые.

    🔴 ЗАЧЕМ (прогон владельца 61a6c634, 25.08.2026). Разворот входов работал
    только для РЕЕСТРОВЫХ формул: своя формула реестру неизвестна по
    устройству, и её входы не заказывал никто. Модель написала
    `ROCE = OPERATING_PROFIT / (TOTAL_ASSETS - TOTAL_CURRENT_LIABILITIES)`, но
    TOTAL_ASSETS в `--metrics` не перечислила — и ROCE не посчитался, как и три
    других коэффициента, которые владелец просил ВСЛУХ. На экран приехали
    четыре пустые строки.

    Требовать этого от модели телом навыка мы уже пробовали («в `--metrics`
    перечисли ВХОДЫ»): она перечисляет их для одних формул и забывает для
    других. `deps` она объявляет ВСЕГДА — иначе формулу не примет валидатор.
    Значит заказ входов выводится из объявленного, а не спрашивается второй раз.
    """
    available_enums, calc_by_enum = (
        _registry_for_profile_id(profile_id) if profile_id else _registry(plan.company_type)
    )
    asked = {str(e).strip().upper() for e in requested if str(e).strip()}
    output_enums = {e for e in asked if e in available_enums}
    known_own = {str(e).strip().upper() for e in (own_deps or ()) if str(e).strip()}
    known_own &= set(available_enums)
    if not output_enums and not known_own:
        # Ни одного знакомого имени И ни одного входа своих формул. Сузить не
        # до чего — отдаём план как был, иначе прогон ушёл бы в пустой набор и
        # вернул пустую таблицу вместо внятного «таких показателей нет».
        return plan
    if not output_enums:
        # 🔴 ЗАКАЗАНЫ ТОЛЬКО СВОИ ФОРМУЛЫ (прогон владельца 31.08.2026).
        #
        # «Посчитай коэффициент автономии и эффективную ставку налога» — оба
        # показателя свои, реестровых слов в запросе нет: `requested` после
        # моста пуст. Ветка сужения не запускалась ВОВСЕ (`if requested:` в
        # build_engine_scope видела пустой список), own_deps не читались, и роль
        # other уводила прогон в ПОЛНУЮ ячейку: человек просил два числа, а
        # получил извлечение 122 показателей на шестнадцать минут и таблицу,
        # которую не заказывал.
        #
        # Правило то же, что у реестрового заказа: читаем входы формул (с их
        # собственными разворотами), показываем — ровно посчитанное. Показ
        # реестровых строк тут пуст НАМЕРЕННО: человек просил коэффициенты, а
        # не их слагаемые; свои строки в показ добавляет расчёт.
        extraction_enums: set[str] = set()
        for enum in known_own:
            extraction_enums.add(enum)
            extraction_enums.update(
                _expand_deps(enum, available_enums=available_enums, calc_by_enum=calc_by_enum)
            )
        return replace(plan, extraction_enums=extraction_enums, output_enums=set())
    extraction_enums = set(output_enums)
    for enum in output_enums:
        extraction_enums.update(
            _expand_deps(enum, available_enums=available_enums, calc_by_enum=calc_by_enum)
        )
    # Входы своих формул — в ЧТЕНИЕ, но не в показ. Разворачиваем и их
    # собственные входы: своя формула законно опирается на реестровый
    # коэффициент, а у того есть свои слагаемые.
    for enum in {str(e).strip().upper() for e in (own_deps or ()) if str(e).strip()}:
        if enum not in available_enums:
            continue  # не наше имя — его заводят через --extra-metrics
        extraction_enums.add(enum)
        extraction_enums.update(
            _expand_deps(enum, available_enums=available_enums, calc_by_enum=calc_by_enum)
        )
    return replace(plan, extraction_enums=extraction_enums, output_enums=output_enums)


def build_engine_scope(
    role: str | None,
    company_type: str | None,
    profile_id: str,
    *,
    basis_full: bool = False,
    requested: "Iterable[str] | None" = None,
    own_deps: "Iterable[str] | None" = None,
) -> tuple[RolePlan, set[str]]:
    """Что движок ЧИТАЕТ из отчёта: (роль-план, набор enum'ов).

    Живёт здесь, а не по копии на каждом пути, намеренно. Решение принимается в
    ДВУХ местах — в бэкенде (`llm/tools.py`) и в станции
    (`station-skills/aifa-extract/scripts/extract.py`), и это ровно та пара, где
    правка одной половины молча разводит dev и прод.

    Две границы, ломать которые нельзя:

    1. РОЛЬ СУЖАЕТ (#141). `build_role_plan` уже отдал «раскладка роли ∪
       рекурсивно развёрнутые входы её формул» — доливать сверху все расчётные
       показатели ячейки незачем. Раньше долив шёл безусловно, и роли ДИЭ к её
       16 показателям приезжало 43 чужих коэффициента; движок эмитит строку
       metric на КАЖДОЕ определение формулы, посчиталось оно или нет, — отсюда
       десятки пустых строк в таблице пользователя.

    2. other/all И НЕПОДДЕРЖИВАЕМАЯ РОЛЬ — полная ячейка ВМЕСТЕ С ФОРМУЛАМИ
       (грабля 31.07.2026). Движок фильтрует по `metric_enums` не только
       извлекаемые, но и расчётные; когда в набор клали одни извлекаемые, из 47
       формул выживало 6 — терялись NET_DEBT, TOTAL_DEBT, RATIO_NET_DEBT_EBITDA,
       EBITDA_MARGIN, FREE_CASH_FLOW, и «долг/EBITDA» анализ считал в уме.

    `basis_full` — исключение из (1): в этом ходу тем же прогоном заземляется
    анализ (#121, grounding_bus), и ему нужен полный базис ячейки. Роль при
    этом никуда не девается — она уезжает в `extract_done` как
    `table_output_enums` и фильтрует ВЫДАЧУ.

    `requested` — ЯВНЫЙ СПИСОК ПОКАЗАТЕЛЕЙ ОТ ЧЕЛОВЕКА (Ш13). Он сильнее роли:
    роль — это догадка о том, что человеку обычно нужно, а список — то, что он
    только что попросил вслух. Правило то же, что у роли, и намеренно: читаем
    запрошенное ВМЕСТЕ с входами его формул (иначе «посчитай рентабельность
    активов» вернёт пустую строку — чистой прибыли и активов в наборе не
    окажется), показываем — ровно запрошенное.
    """
    plan = build_role_plan(role, company_type, profile_id)
    # `or own_deps`: заказ ТОЛЬКО из своих формул — тоже заказ. Без этого пустой
    # `requested` (все слова оказались своими) пролетал мимо сужения целиком.
    if requested or own_deps:
        plan = _narrow_to_requested(plan, requested or (), profile_id=profile_id,
                                    own_deps=own_deps)
        if plan.extraction_enums and not basis_full:
            return plan, set(plan.extraction_enums)
    if plan.extraction_enums and not basis_full:
        return plan, set(plan.extraction_enums)
    try:
        from finengine.skills.metric_schema import registry_for_profile

        reg = registry_for_profile(profile_id)
        full = {str(m["enum"]).strip() for m in reg["extractable"]}
        full |= {str(c[0]).strip() for c in reg["calculated"]}
    # ТОЛЬКО «файла ячейки нет» (#153). Было `except Exception`: с реестром,
    # вынесенным в данные, сюда проваливался бы и СЛОМАННЫЙ внешний файл —
    # состав молча съезжал бы на базу по типу компании, а прогон выглядел бы
    # удавшимся. Порча реестра обязана быть отказом, а не тихой заменой.
    except FileNotFoundError:
        full, _calc = _registry(company_type)  # _registry уже отдаёт объединение
        full = set(full)
    full.discard("")
    return plan, full
