import calendar
from dataclasses import dataclass
import logging
import re
from copy import deepcopy

from finengine.skills.periods import (
    DURATION_KIND,
    FiscalPeriodContext,
    POINT_IN_TIME_ENUMS,
    POINT_KIND,
    normalize_period_label,
    period_kind_for_enum,
)

logger = logging.getLogger(__name__)

# Kept as compatibility names for the copied validation helpers. The source of
# truth lives in `periods.py`; duration is the fallback for enums not listed as
# point-in-time.
POINT_IN_TIME_PARAMETERS = POINT_IN_TIME_ENUMS
DURATION_PARAMETERS = frozenset()


MONTHS_RU = {
    "января": "01",
    "февраля": "02",
    "марта": "03",
    "апреля": "04",
    "мая": "05",
    "июня": "06",
    "июля": "07",
    "августа": "08",
    "сентября": "09",
    "октября": "10",
    "ноября": "11",
    "декабря": "12",
}

MONTHS_EN = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}


REVIEW_REASONS = {
    "raw_kind_mismatch_but_period_boundary_matches",
    "period_raw_duration_granularity_mismatch_kept_for_review",
}


@dataclass(frozen=True)
class PeriodValidationResult:
    ok: bool
    period: str
    reason: str
    expected_kind: str | None
    raw_kind: str | None


_RU_MONTH_TO_NUMBER = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}


def _extract_period_end_month_year(raw_period: str) -> tuple[int | None, int | None]:
    if not raw_period:
        return None, None

    text = raw_period.strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)

    # 30.06.2025 / 30-06-2025 / 30/06/2025
    numeric_date_match = re.search(
        r"(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{4})",
        text,
    )
    if numeric_date_match:
        return int(numeric_date_match.group("month")), int(numeric_date_match.group("year"))

    # English dates: March 31, 2026 / December 31, 2025
    en_date_match = re.search(
        r"(?P<month>"
        + "|".join(MONTHS_EN.keys())
        + r")\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})",
        text,
    )
    if en_date_match:
        month = int(MONTHS_EN[en_date_match.group("month")])
        year = int(en_date_match.group("year"))
        return month, year

    # 30 июня 2025 г.
    ru_date_match = re.search(
        r"(?P<day>\d{1,2})\s+"
        r"(?P<month>января|январь|февраля|февраль|марта|март|апреля|апрель|мая|май|июня|июнь|июля|июль|августа|август|сентября|сентябрь|октября|октябрь|ноября|ноябрь|декабря|декабрь)"
        r"\s+(?P<year>\d{4})",
        text,
    )
    if ru_date_match:
        month = _RU_MONTH_TO_NUMBER[ru_date_match.group("month")]
        year = int(ru_date_match.group("year"))
        return month, year

    year_match = re.search(r"(?P<year>20\d{2}|19\d{2})", text)
    if year_match:
        return None, int(year_match.group("year"))

    return None, None


def _quarter_from_end_month(month: int) -> int | None:
    if month in {1, 2, 3}:
        return 1
    if month in {4, 5, 6}:
        return 2
    if month in {7, 8, 9}:
        return 3
    if month in {10, 11, 12}:
        return 4
    return None


def _get_attr(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _set_attr(obj, name, value):
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


def _parameter_type_to_str(parameter_type) -> str:
    if parameter_type is None:
        return ""

    # ParameterEnum.X -> X
    value = getattr(parameter_type, "value", None)
    if value:
        return str(value)

    name = getattr(parameter_type, "name", None)
    if name:
        return str(name)

    return str(parameter_type)


def _is_point_in_time_period(period: str) -> bool:
    if not period:
        return False
    return bool(re.fullmatch(r"\d{1,2}\.\d{2}\.\d{4}", period.strip()))


def _is_duration_period(period: str) -> bool:
    if not period:
        return False

    period = period.strip()

    return bool(
        re.fullmatch(r"Q[1-4]\s+\d{4}", period)
        or re.fullmatch(r"[69]M\s+\d{4}", period)
        or re.fullmatch(r"FY\s+\d{4}", period)
        or re.fullmatch(r"\d{1,2}M\s+\d{1,2}\.\d{2}\.\d{4}", period)
    )


def _expected_period_kind(parameter_type: str) -> str | None:
    if not parameter_type:
        return None
    return period_kind_for_enum(parameter_type)


def _actual_period_kind(period: str) -> str | None:
    if _is_point_in_time_period(period):
        return "point"
    if _is_duration_period(period):
        return "duration"
    return None


def _raw_period_kind(
    raw_period: str,
    *,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str | None:
    if not raw_period:
        return None

    raw_period = raw_period.strip()

    if _normalize_duration_from_raw(raw_period, fiscal_context=fiscal_context):
        return "duration"

    if _normalize_date_from_raw(raw_period, fiscal_context=fiscal_context):
        return "point"

    return None


def _normalized_raw_period_for_kind(
    raw_period: str,
    expected_kind: str,
    *,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str | None:
    if expected_kind == POINT_KIND:
        return _normalize_date_from_raw(raw_period, fiscal_context=fiscal_context)

    if expected_kind == DURATION_KIND:
        return _normalize_duration_from_raw(raw_period, fiscal_context=fiscal_context)

    return None


def _period_year(period: str) -> str | None:
    if not period:
        return None

    match = re.search(r"\b(20\d{2}|19\d{2})\b", period)
    if match:
        return match.group(1)

    if _is_point_in_time_period(period):
        return period[-4:]

    return None


def _duration_boundary_date(period: str) -> str | None:
    if not period:
        return None

    period = period.strip()

    match = re.fullmatch(r"FY\s+(20\d{2}|19\d{2})", period)
    if match:
        return f"31.12.{match.group(1)}"

    match = re.fullmatch(r"9M\s+(20\d{2}|19\d{2})", period)
    if match:
        return f"30.09.{match.group(1)}"

    match = re.fullmatch(r"6M\s+(20\d{2}|19\d{2})", period)
    if match:
        return f"30.06.{match.group(1)}"

    match = re.fullmatch(r"Q([1-4])\s+(20\d{2}|19\d{2})", period)
    if match:
        quarter = int(match.group(1))
        year = match.group(2)
        month_by_quarter = {
            1: 3,
            2: 6,
            3: 9,
            4: 12,
        }
        month = month_by_quarter[quarter]
        day = calendar.monthrange(int(year), month)[1]
        return f"{day:02d}.{month:02d}.{year}"

    match = re.fullmatch(r"(\d{1,2})M\s+(\d{1,2})\.(\d{2})\.(20\d{2}|19\d{2})", period)
    if match:
        _months, day, end_month, year = match.groups()
        return f"{int(day):02d}.{int(end_month):02d}.{year}"

    return None


def _period_boundary_date(period: str) -> str | None:
    kind = _actual_period_kind(period)

    if kind == "point":
        return period.strip()

    if kind == "duration":
        return _duration_boundary_date(period)

    return None


def _periods_have_same_boundary(left_period: str, right_period: str) -> bool:
    left_boundary = _period_boundary_date(left_period)
    right_boundary = _period_boundary_date(right_period)

    return bool(left_boundary and right_boundary and left_boundary == right_boundary)


def _duration_periods_have_same_year(left_period: str, right_period: str) -> bool:
    if _actual_period_kind(left_period) != "duration":
        return False
    if _actual_period_kind(right_period) != "duration":
        return False

    left_year = _period_year(left_period)
    right_year = _period_year(right_period)

    return bool(left_year and right_year and left_year == right_year)


def _period_is_consistent_with_raw(
    *,
    period: str,
    raw_period: str,
    expected_kind: str,
    fiscal_context: FiscalPeriodContext | None = None,
) -> tuple[bool, str | None, str]:
    actual_kind = _actual_period_kind(period)
    raw_kind = _raw_period_kind(raw_period, fiscal_context=fiscal_context)

    normalized_raw_for_expected = _normalized_raw_period_for_kind(
        raw_period,
        expected_kind,
        fiscal_context=fiscal_context,
    )
    normalized_raw_for_actual = (
        _normalized_raw_period_for_kind(raw_period, actual_kind, fiscal_context=fiscal_context)
        if actual_kind is not None
        else None
    )

    # raw_period явно согласован с ожидаемым типом метрики.
    # Если period другого типа, это безопасный repair по raw_period.
    if raw_kind == expected_kind:
        if normalized_raw_for_expected and actual_kind != expected_kind:
            return True, normalized_raw_for_expected, "period_kind_repaired_from_raw"

        if normalized_raw_for_expected and normalized_raw_for_expected != period:
            # Спорный duration-кейс: например period=FY 2021, raw_period="за 9 месяцев 2021".
            # Пока сохраняем для проверки на валидационной выборке.
            if (
                expected_kind == "duration"
                and actual_kind == "duration"
                and _duration_periods_have_same_year(period, normalized_raw_for_expected)
            ):
                return True, None, "period_raw_duration_granularity_mismatch_kept_for_review"

            return False, None, f"period_raw_boundary_mismatch: raw_normalized={normalized_raw_for_expected!r}"

        if actual_kind == expected_kind:
            return True, None, "ok"

        return False, None, "period_kind_mismatch"

    # raw_period не удалось классифицировать.
    # Если period сам согласован с типом метрики, оставляем.
    if raw_kind is None:
        if actual_kind == expected_kind:
            return True, None, "ok_raw_kind_unknown"

        repaired = _repair_period_for_expected_kind(
            period=period,
            raw_period=raw_period,
            expected_kind=expected_kind,
            fiscal_context=fiscal_context,
        )
        if repaired and _actual_period_kind(repaired) == expected_kind:
            return True, repaired, "period_kind_repaired"

        return False, None, "period_kind_mismatch_raw_kind_unknown"

    # raw_period классифицирован, но его kind отличается от ожидаемого типа метрики.
    # Если period уже согласован с типом метрики и граница периода совпадает с raw,
    # сохраняем, но логируем: вероятно, LLM потеряла "за период" или наоборот.
    if actual_kind == expected_kind:
        if normalized_raw_for_actual and _periods_have_same_boundary(period, normalized_raw_for_actual):
            return True, None, "raw_kind_mismatch_but_period_boundary_matches"

        return False, None, "period_raw_boundary_mismatch"

    # period и raw_period одного kind, но оба противоречат expected_kind.
    # Это опасно: LLM могла выбрать значение из таблицы другого типа.
    # Например CASH_EQUIVALENTS с period=FY 2020 и raw_period="За год..."
    # не надо сохранять как suspicious, его надо дропать.
    if actual_kind == raw_kind:
        if normalized_raw_for_actual is None or normalized_raw_for_actual == period:
            return False, None, "period_and_raw_kind_match_but_expected_kind_differs"

        return False, None, f"period_raw_boundary_mismatch: raw_normalized={normalized_raw_for_actual!r}"

    return False, None, "period_raw_expected_kind_mismatch"


def _normalize_date_from_raw(
    raw_period: str,
    *,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str | None:
    if not raw_period:
        return None

    normalized = normalize_period_label(
        raw_period,
        value_kind=POINT_KIND,
        fiscal_context=fiscal_context,
    )
    return normalized if _is_point_in_time_period(normalized) else None


def _normalize_duration_from_raw(
    raw_period: str,
    *,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str | None:
    if not raw_period:
        return None

    normalized = normalize_period_label(
        raw_period,
        value_kind=DURATION_KIND,
        fiscal_context=fiscal_context,
    )
    return normalized if _is_duration_period(normalized) else None


def _repair_period_for_expected_kind(
    period: str,
    raw_period: str,
    expected_kind: str,
    *,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str | None:
    if expected_kind == POINT_KIND:
        return _normalize_date_from_raw(raw_period, fiscal_context=fiscal_context)

    if expected_kind == DURATION_KIND:
        return _normalize_duration_from_raw(raw_period, fiscal_context=fiscal_context)

    return None


def normalize_metric_period(
    raw_period: object,
    enum: object,
    *,
    normalized_period: object | None = None,
    drop_unrepairable: bool = False,
    fiscal_context: FiscalPeriodContext | None = None,
) -> PeriodValidationResult:
    """Normalize and validate a metric period against its enum period kind.

    The LLM is trusted only for the raw table/header text. If a caller also has
    a model-normalized period, it is used as a consistency signal, not as the
    source of truth. Point-in-time metrics are coerced to `DD.MM.YYYY`; duration
    metrics are coerced to `FY/Q/6M/9M/12M/3M ...` labels.
    """
    raw_text = str(raw_period or "").strip()
    expected_kind = _expected_period_kind(_parameter_type_to_str(enum))
    raw_kind = _raw_period_kind(raw_text, fiscal_context=fiscal_context)
    if expected_kind is None:
        fallback = normalize_period_label(raw_text, fiscal_context=fiscal_context)
        return PeriodValidationResult(
            ok=bool(fallback),
            period=fallback,
            reason="unknown_enum_kind",
            expected_kind=None,
            raw_kind=raw_kind,
        )

    normalized_from_raw = _normalized_raw_period_for_kind(
        raw_text,
        expected_kind,
        fiscal_context=fiscal_context,
    )
    if normalized_from_raw:
        return PeriodValidationResult(
            ok=True,
            period=normalized_from_raw,
            reason="raw_normalized_for_expected_kind",
            expected_kind=expected_kind,
            raw_kind=raw_kind,
        )

    normalized_text = str(normalized_period or "").strip()
    if normalized_text:
        candidate = normalize_period_label(
            normalized_text,
            value_kind=expected_kind,
            fiscal_context=fiscal_context,
        )
        ok, repaired, reason = _period_is_consistent_with_raw(
            period=candidate,
            raw_period=raw_text,
            expected_kind=expected_kind,
            fiscal_context=fiscal_context,
        )
        if ok:
            return PeriodValidationResult(
                ok=True,
                period=repaired or candidate,
                reason=reason,
                expected_kind=expected_kind,
                raw_kind=raw_kind,
            )
        if drop_unrepairable:
            return PeriodValidationResult(
                ok=False,
                period="",
                reason=reason,
                expected_kind=expected_kind,
                raw_kind=raw_kind,
            )

    fallback = normalize_period_label(
        raw_text,
        value_kind=expected_kind,
        fiscal_context=fiscal_context,
    )
    if fallback and _actual_period_kind(fallback) == expected_kind:
        return PeriodValidationResult(
            ok=True,
            period=fallback,
            reason="period_kind_inferred_from_raw",
            expected_kind=expected_kind,
            raw_kind=raw_kind,
        )

    return PeriodValidationResult(
        ok=not drop_unrepairable and bool(fallback),
        period=fallback if not drop_unrepairable else "",
        reason="period_kind_unrepairable",
        expected_kind=expected_kind,
        raw_kind=raw_kind,
    )


def _filter_empty_period_blocks(document_parameters):
    filtered = []

    for period_block in document_parameters:
        extracted = _get_attr(period_block, "extracted", []) or []
        if extracted:
            filtered.append(period_block)

    return filtered


def _is_interim_duration_period(period: str) -> bool:
    if not period:
        return False

    period = period.strip()

    return bool(
        re.fullmatch(r"Q[1-4]\s+\d{4}", period)
        or re.fullmatch(r"[69]M\s+\d{4}", period)
        or re.fullmatch(r"\d{1,2}M\s+\d{1,2}\.\d{2}\.\d{4}", period)
    )


def _is_fy_period(period: str) -> bool:
    if not period:
        return False

    return bool(re.fullmatch(r"FY\s+\d{4}", period.strip()))


def _drop_cross_period_duration_value_duplicates(document_parameters):
    """
    Conservative guard for internally consistent but suspicious LLM outputs.

    Example:
      INTEREST_PAID value=19608 appears both as:
        - 9M 2021 / "За 9 месяцев..."
        - FY 2020 / "За год..."
      In interim statements this usually means the LLM copied the same value
      from an interim cash-flow table and hallucinated an FY period.

    Rule:
      For the same parameter type and same value, if both interim duration
      periods and FY periods are present, drop FY duplicates and keep interim.
    """
    value_locations = {}

    for block_idx, period_block in enumerate(document_parameters):
        period = _get_attr(period_block, "period")
        raw_period = _get_attr(period_block, "raw_period")
        extracted_items = _get_attr(period_block, "extracted", []) or []

        for item_idx, item in enumerate(extracted_items):
            parameter_type = _parameter_type_to_str(_get_attr(item, "type"))
            expected_kind = _expected_period_kind(parameter_type)
            value = _get_attr(item, "value")

            if expected_kind != "duration":
                continue

            if value in (None, "", "Не найдено"):
                continue

            if not _is_duration_period(period):
                continue

            key = (parameter_type, str(value))
            value_locations.setdefault(key, []).append(
                {
                    "block_idx": block_idx,
                    "item_idx": item_idx,
                    "period": period,
                    "raw_period": raw_period,
                    "item": item,
                }
            )

    items_to_drop_by_block = {}

    for (parameter_type, value), locations in value_locations.items():
        if len(locations) < 2:
            continue

        has_interim = any(_is_interim_duration_period(location["period"]) for location in locations)
        has_fy = any(_is_fy_period(location["period"]) for location in locations)

        if not has_interim or not has_fy:
            continue

        for location in locations:
            if not _is_fy_period(location["period"]):
                continue

            items_to_drop_by_block.setdefault(location["block_idx"], set()).add(location["item_idx"])

            logger.warning(
                "Dropped suspicious cross-period duplicate duration value: "
                f"type={parameter_type}, value={value!r}, period={location['period']!r}, "
                f"raw_period={location['raw_period']!r}; same value also appears in interim period"
            )

    if not items_to_drop_by_block:
        return document_parameters

    filtered_parameters = []

    for block_idx, period_block in enumerate(document_parameters):
        drop_item_indexes = items_to_drop_by_block.get(block_idx, set())
        extracted_items = _get_attr(period_block, "extracted", []) or []

        if not drop_item_indexes:
            filtered_parameters.append(period_block)
            continue

        kept_items = [
            item
            for item_idx, item in enumerate(extracted_items)
            if item_idx not in drop_item_indexes
        ]

        if kept_items:
            _set_attr(period_block, "extracted", kept_items)
            filtered_parameters.append(period_block)

    return _filter_empty_period_blocks(filtered_parameters)


def repair_document_parameters_periods(
    document_parameters_list,
    enabled: bool = True,
    drop_unrepairable: bool = True,
):
    if not enabled:
        return document_parameters_list

    repaired_sources = deepcopy(document_parameters_list)

    for source in repaired_sources:
        parameters = _get_attr(source, "parameters", None)
        if parameters is None:
            continue

        new_parameters = []

        for period_block in parameters:
            block_period = _get_attr(period_block, "period")
            block_raw_period = _get_attr(period_block, "raw_period")

            extracted_items = _get_attr(period_block, "extracted", []) or []
            kept_in_original_block = []

            # Ключ: (period, raw_period)
            # Значение: список extracted items, которые надо вынести в новый block.
            moved_items_by_period = {}

            for item in extracted_items:
                parameter_type = _parameter_type_to_str(_get_attr(item, "type"))
                expected_kind = _expected_period_kind(parameter_type)

                item_raw_period = _get_attr(item, "raw_period", None) or block_raw_period
                item_period = block_period

                value = _get_attr(item, "value")
                if value in (None, "", "Не найдено"):
                    if drop_unrepairable:
                        logger.warning(
                            "Dropped empty extracted value: "
                            f"type={parameter_type}, period={item_period!r}, raw_period={item_raw_period!r}"
                        )
                        continue

                    kept_in_original_block.append(item)
                    continue

                if expected_kind is None:
                    kept_in_original_block.append(item)
                    continue

                ok, repaired_period, reason = _period_is_consistent_with_raw(
                    period=item_period,
                    raw_period=item_raw_period,
                    expected_kind=expected_kind,
                )

                if ok:
                    if repaired_period and repaired_period != block_period:
                        logger.warning(
                            "Repaired period mismatch: "
                            f"type={parameter_type}, period={item_period!r}, raw_period={item_raw_period!r}, "
                            f"expected={expected_kind}, repaired={repaired_period!r}, reason={reason}"
                        )

                        moved_key = (repaired_period, item_raw_period)
                        moved_items_by_period.setdefault(moved_key, []).append(item)
                    else:
                        if reason in REVIEW_REASONS:
                            logger.warning(
                                "Kept suspicious period/raw_period mismatch for review: "
                                f"type={parameter_type}, period={item_period!r}, raw_period={item_raw_period!r}, "
                                f"expected={expected_kind}, reason={reason}"
                            )

                        kept_in_original_block.append(item)

                    continue

                message = (
                    "Unrepairable period/raw_period mismatch: "
                    f"type={parameter_type}, period={item_period!r}, raw_period={item_raw_period!r}, "
                    f"expected={expected_kind}, reason={reason}"
                )

                if drop_unrepairable:
                    logger.warning(message + " — dropped")
                    continue

                logger.warning(message + " — kept")
                kept_in_original_block.append(item)

            if kept_in_original_block:
                _set_attr(period_block, "extracted", kept_in_original_block)
                new_parameters.append(period_block)

            for (repaired_period, repaired_raw_period), moved_items in moved_items_by_period.items():
                repaired_block = deepcopy(period_block)
                _set_attr(repaired_block, "period", repaired_period)
                _set_attr(repaired_block, "raw_period", repaired_raw_period)
                _set_attr(repaired_block, "extracted", moved_items)
                new_parameters.append(repaired_block)

        new_parameters = _filter_empty_period_blocks(new_parameters)
        new_parameters = _drop_cross_period_duration_value_duplicates(new_parameters)
        _set_attr(source, "parameters", new_parameters)

        new_periods = []
        for period_block in new_parameters:
            period = _get_attr(period_block, "period")
            if period and period not in new_periods:
                new_periods.append(period)

        _set_attr(source, "periods", new_periods)

    return repaired_sources
