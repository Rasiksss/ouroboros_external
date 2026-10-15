from __future__ import annotations

import math
import re
from typing import Any, Callable

from finengine.skills.bulk_extractor import (
    _MissingFormulaInput,
    _eval_formula_expr,
    _period_boundary_keys,
    _point_period_key,
    _replace_avg_calls,
)
from finengine.skills.metric_registry import EXTRACTABLE_BANK
from finengine.skills.periods import normalize_period_label, sort_periods


DGR_ALERT_ANNUALIZED = "annualized"
DGR_ALERT_SINGLE_POINT_AVG = "single_point_avg"

_FORMULA_DEFAULT_ABS_ENUMS = {
    str(metric.get("enum") or "").strip().upper()
    for metric in EXTRACTABLE_BANK
    if metric.get("formula_default_abs_behavior")
}


def apply_dgr_calculated_adjustments(
    metric: dict[str, Any],
    previous_metrics: list[dict[str, Any]],
    periods: list[str] | None = None,
    *,
    period_current: str = "",
    period_prev: str = "",
) -> dict[str, Any]:
    """DGR-only post-processing for calculated AVG metrics.

    The core extractor deliberately keeps generic accounting math role-neutral.
    DGR wants annualized ratios for short flow periods and a visible warning
    when an AVG denominator has only one boundary point available.
    """
    formula = str(metric.get("formula") or "")
    if not metric.get("is_calculated") or not re.search(r"\bAVG\s*\(", formula, re.I):
        return metric

    adjusted = False
    out = dict(metric)
    period_values = [
        dict(pv)
        for pv in (metric.get("period_values") or [])
        if isinstance(pv, dict)
    ]

    for pv in period_values:
        period = _normalized_period(pv.get("period"))
        factor = _annualization_factor(period)
        if factor is None or _has_alert(pv, DGR_ALERT_ANNUALIZED):
            continue
        value = _to_float(pv.get("value"))
        if value is None:
            continue
        pv["value"] = value * factor
        _append_alert(pv, _annualized_alert(period, factor))
        adjusted = True

    existing_periods = {_normalized_period(pv.get("period")) for pv in period_values}
    env_by_period = _env_by_period(previous_metrics)
    point_envs = _point_envs(env_by_period)
    target_periods = _candidate_periods(periods or [], previous_metrics, period_values)

    for period in target_periods:
        if period in existing_periods or _period_boundary_keys(period) is None:
            continue
        current_env = env_by_period.get(period)
        if not current_env:
            continue
        resolver, single_point_uses = _single_point_avg_resolver(period, env_by_period, point_envs)
        try:
            expr = _replace_avg_calls(formula, current_env, None, resolver)
            value = _eval_formula_expr(expr, current_env)
        except Exception:
            continue
        if not single_point_uses:
            continue

        alerts = [_single_point_alert(use) for use in single_point_uses]
        factor = _annualization_factor(period)
        if factor is not None:
            value *= factor
            alerts.append(_annualized_alert(period, factor))
        period_values.append(
            {
                "period": period,
                "value": value,
                "unit": metric.get("unit") or "",
                "page": None,
                "source_text": "",
                "alerts": alerts,
            }
        )
        existing_periods.add(period)
        adjusted = True

    if not adjusted:
        return metric

    out["period_values"] = _sort_period_values(period_values)
    _refresh_legacy_values(out, period_current=period_current, period_prev=period_prev)
    out["found"] = any(_value_present(pv.get("value")) for pv in out["period_values"])
    return out


def _normalized_period(period: object) -> str:
    return normalize_period_label(period)


def _value_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if not isinstance(value, str):
        return None
    text = (
        value.strip()
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("%", "")
    )
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text:
        text = text.replace(",", "")
    try:
        number = float(text)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _annualization_factor(period: object) -> float | None:
    p = _normalized_period(period)
    if re.fullmatch(r"Q[1-4] 20\d{2}", p):
        return 4.0
    match = re.fullmatch(r"(\d{1,2})M (?:20\d{2}|\d{1,2}[.]\d{1,2}[.]20\d{2})", p)
    if match:
        months = int(match.group(1))
        if months in {3, 6, 9}:
            return 12.0 / months
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})M 20\d{2}", p)
    if match:
        start, end = map(int, match.groups())
        months = end - start + 1
        if months in {3, 6, 9}:
            return 12.0 / months
    return None


def _factor_label(factor: float) -> str:
    if abs(factor - (4.0 / 3.0)) < 1e-9:
        return "4/3"
    if float(factor).is_integer():
        return str(int(factor))
    return f"{factor:.2f}".rstrip("0").rstrip(".")


def _annualized_alert(period: str, factor: float) -> dict[str, Any]:
    # `period` and `factor_label` are carried as fields, not just baked into
    # `message`, so the UI can render the alert in its own language instead
    # of showing this sentence verbatim.
    return {
        "code": DGR_ALERT_ANNUALIZED,
        "message": f"Показатель аннуализирован: значение за {period} умножено на {_factor_label(factor)}.",
        "factor": factor,
        "factor_label": _factor_label(factor),
        "period": period,
    }


def _single_point_alert(use: dict[str, Any]) -> dict[str, Any]:
    used_period = str(use.get("used_period") or "")
    missing_period = str(use.get("missing_period") or "")
    message = (
        "Расчет выполнен без усреднения: доступна только одна из двух point-дат"
        f" для AVG ({used_period})."
    )
    if missing_period:
        message += f" Не найдена граничная дата {missing_period}."
    return {
        "code": DGR_ALERT_SINGLE_POINT_AVG,
        "message": message,
        "used_period": used_period,
        "missing_period": missing_period,
    }


def _has_alert(pv: dict[str, Any], code: str) -> bool:
    return any(isinstance(alert, dict) and alert.get("code") == code for alert in pv.get("alerts") or [])


def _append_alert(pv: dict[str, Any], alert: dict[str, Any]) -> None:
    alerts = [a for a in pv.get("alerts") or [] if isinstance(a, dict)]
    code = alert.get("code")
    if code and any(a.get("code") == code for a in alerts):
        pv["alerts"] = alerts
        return
    alerts.append(alert)
    pv["alerts"] = alerts


def _env_by_period(metrics: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for metric in metrics:
        enum = str(metric.get("enum") or "").strip().upper()
        if not enum:
            continue
        is_calculated = bool(metric.get("is_calculated"))
        for pv in metric.get("period_values") or []:
            if not isinstance(pv, dict):
                continue
            period = _normalized_period(pv.get("period"))
            if not period:
                continue
            value = _to_float(pv.get("value"))
            if value is None:
                continue
            if not is_calculated and enum in _FORMULA_DEFAULT_ABS_ENUMS:
                value = abs(value)
            out.setdefault(period, {})[enum] = value
    return out


def _point_envs(env_by_period: dict[str, dict[str, float]]) -> dict[tuple[int, int, int], tuple[str, dict[str, float]]]:
    out: dict[tuple[int, int, int], tuple[str, dict[str, float]]] = {}
    for period, env in env_by_period.items():
        key = _point_period_key(period)
        if key is not None:
            out.setdefault(key, (period, env))
    return out


def _single_point_avg_resolver(
    target_period: str,
    env_by_period: dict[str, dict[str, float]],
    point_envs: dict[tuple[int, int, int], tuple[str, dict[str, float]]],
) -> tuple[Callable[[str], float], list[dict[str, Any]]]:
    uses: list[dict[str, Any]] = []

    def resolve(inner_expr: str) -> float:
        boundary_keys = _period_boundary_keys(target_period)
        if boundary_keys is None:
            raise _MissingFormulaInput("AVG boundary point periods")
        values: list[tuple[str, float]] = []
        missing: list[tuple[int, int, int]] = []
        for boundary_key in boundary_keys:
            point = point_envs.get(boundary_key)
            if point is None:
                missing.append(boundary_key)
                continue
            point_period, point_env = point
            try:
                values.append((point_period, _eval_formula_expr(inner_expr, point_env)))
            except Exception:
                missing.append(boundary_key)
        if len(values) == 2:
            return (values[0][1] + values[1][1]) / 2.0
        if len(values) == 1:
            uses.append(
                {
                    "used_period": values[0][0],
                    "missing_period": _format_boundary_key(missing[0]) if missing else "",
                }
            )
            return values[0][1]
        raise _MissingFormulaInput("AVG point periods")

    return resolve, uses


def _format_boundary_key(key: tuple[int, int, int]) -> str:
    year, month, day = key
    return f"{day:02d}.{month:02d}.{year}"


def _candidate_periods(
    periods: list[str],
    previous_metrics: list[dict[str, Any]],
    period_values: list[dict[str, Any]],
) -> list[str]:
    candidates: set[str] = set()
    for period in periods:
        normalized = _normalized_period(period)
        if normalized:
            candidates.add(normalized)
    for source in (previous_metrics, [{"period_values": period_values}]):
        for metric in source:
            for pv in metric.get("period_values") or []:
                if not isinstance(pv, dict):
                    continue
                normalized = _normalized_period(pv.get("period"))
                if normalized:
                    candidates.add(normalized)
    return [
        period
        for period in sort_periods(list(candidates))
        if _point_period_key(period) is None and _period_boundary_keys(period) is not None
    ]


def _sort_period_values(period_values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_period = {_normalized_period(pv.get("period")): pv for pv in period_values}
    ordered = sort_periods(list(by_period))
    return [by_period[period] for period in ordered]


def _refresh_legacy_values(
    metric: dict[str, Any],
    *,
    period_current: str = "",
    period_prev: str = "",
) -> None:
    by_period = {
        _normalized_period(pv.get("period")): pv.get("value")
        for pv in metric.get("period_values") or []
        if isinstance(pv, dict)
    }
    current_key = _normalized_period(period_current)
    prev_key = _normalized_period(period_prev)
    if current_key and current_key in by_period:
        metric["value_current"] = by_period[current_key]
    if prev_key and prev_key in by_period:
        metric["value_prev"] = by_period[prev_key]
