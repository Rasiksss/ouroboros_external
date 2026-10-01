"""Query plan validation and Navigator condition serialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import constants as catalog, structure_catalog as live_catalog
from .structure_catalog import column_names_from_model_node


class PlanValidationError(Exception):
    def __init__(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ValidatedQueryPlan:
    model_sname: str
    n_id: int
    conditions: list[dict[str, Any]]
    limit: int
    applied_conditions: list[dict[str, Any]]


def query_limit(value: Any, *, default: int = catalog.DEFAULT_ROW_LIMIT, maximum: int = catalog.MAX_ROW_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def resolve_model(model: str, *, structure_catalog: live_catalog.StructureCatalog) -> live_catalog.NavigatorModelInfo:
    text = str(model or "").strip()
    if not text:
        raise PlanValidationError(
            "needs_clarification",
            "Unknown model. Call mode=schema for the configured model catalog.",
        )

    try:
        return live_catalog.resolve_model_input(text, structure_catalog)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("ambiguous:"):
            raise PlanValidationError(
                "needs_clarification",
                f"Ambiguous model {text!r}. Candidates: {message.removeprefix('ambiguous: ')}.",
            ) from exc
        raise PlanValidationError(
            "needs_clarification",
            f"Unknown model {text!r}. Call mode=schema for the configured model catalog.",
        ) from exc


def validate_query_plan(
    plan: Any,
    *,
    structure_catalog: live_catalog.StructureCatalog,
    query_mode: bool = True,
) -> ValidatedQueryPlan:
    if not isinstance(plan, dict):
        raise PlanValidationError("error", "plan must be an object.")

    unknown_keys = set(plan) - {"model", "conditions", "limit"}
    if unknown_keys:
        joined = ", ".join(sorted(unknown_keys))
        raise PlanValidationError("error", f"Unknown plan keys: {joined}.")

    model_text = str(plan.get("model") or "")
    try:
        info = live_catalog.resolve_model_input(model_text, structure_catalog)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("ambiguous:"):
            raise PlanValidationError(
                "needs_clarification",
                f"Ambiguous model {model_text!r}. Candidates: {message.removeprefix('ambiguous: ')}.",
            ) from exc
        raise PlanValidationError(
            "needs_clarification",
            f"Unknown model {model_text!r}. Call mode=schema for the configured model catalog.",
        ) from exc

    allowed_columns = column_names_from_model_node(info.raw)

    raw_conditions = plan.get("conditions") or []
    if not isinstance(raw_conditions, list):
        raise PlanValidationError("error", "plan.conditions must be a list.")
    if len(raw_conditions) > catalog.MAX_CONDITIONS:
        raise PlanValidationError("error", f"Too many conditions (max {catalog.MAX_CONDITIONS}).")

    normalized_conditions: list[dict[str, Any]] = []
    applied_conditions: list[dict[str, Any]] = []
    for item in raw_conditions:
        normalized_conditions.append(
            _normalize_condition(item, model_sname=info.s_name, allowed_columns=allowed_columns)
        )
        applied_conditions.append(_applied_condition(normalized_conditions[-1]))

    if query_mode and not normalized_conditions:
        raise PlanValidationError(
            "needs_clarification",
            "At least one filter condition is required for a bounded query.",
        )

    row_limit = query_limit(plan.get("limit"))
    return ValidatedQueryPlan(
        model_sname=info.s_name,
        n_id=info.n_id,
        conditions=normalized_conditions,
        limit=row_limit,
        applied_conditions=applied_conditions,
    )


def dataset_request_body(validated: ValidatedQueryPlan) -> dict[str, Any]:
    return {
        "dataset": {
            "sSource": str(validated.n_id),
            "sType": "dm",
            "conditions": {"r": [navigator_condition(condition) for condition in validated.conditions]},
        }
    }


def navigator_condition(condition: dict[str, Any]) -> dict[str, str]:
    operator = str(condition["operator"])
    wire: dict[str, str] = {
        "sOperator": operator,
        "sLeftType": "column",
        "sLeftExp": str(condition["column"]),
        "sRightType": "const",
        "isDisabled": "0",
    }
    if operator in catalog.NULL_OPERATORS:
        wire["sRightExp"] = ""
        return wire
    value = condition.get("value")
    if operator in catalog.LIST_OPERATORS:
        values = value if isinstance(value, list) else [value]
        wire["sRightExp"] = catalog.IN_VALUE_SEPARATOR.join(str(item) for item in values)
        return wire
    wire["sRightExp"] = str(value)
    return wire


def _normalize_operator(raw: Any) -> str:
    operator = " ".join(str(raw or "").strip().casefold().split())
    if operator == "not in":
        return "not in"
    if operator == "is null":
        return "is null"
    if operator == "is not null":
        return "is not null"
    return operator


def _normalize_condition(
    item: Any,
    *,
    model_sname: str,
    allowed_columns: set[str] | None,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise PlanValidationError("error", "Each condition must be an object.")
    if item.get("type") in {"and", "or", "not"} or "conditions" in item:
        raise PlanValidationError("error", "Only flat AND conditions are supported.")

    column = str(item.get("column") or "").strip()
    if not column:
        raise PlanValidationError("needs_clarification", "Condition column is required.")
    if allowed_columns is not None and column not in allowed_columns:
        raise PlanValidationError(
            "needs_clarification",
            f"Column {column!r} is not allowed for {model_sname}. Request schema for this model.",
        )

    operator = _normalize_operator(item.get("operator"))
    if operator not in catalog.ALLOWED_OPERATORS:
        raise PlanValidationError("error", f"Operator {operator!r} is not allowed.")

    value = item.get("value")
    if operator in catalog.NULL_OPERATORS:
        return {"column": column, "operator": operator}

    if operator in catalog.LIST_OPERATORS:
        values = _normalize_list_values(value, operator=operator)
        if not values:
            raise PlanValidationError("needs_clarification", f"Operator {operator!r} requires at least one value.")
        if len(values) > catalog.MAX_IN_VALUES:
            raise PlanValidationError("error", f"Too many values for {operator!r} (max {catalog.MAX_IN_VALUES}).")
        _validate_scalar_lengths(values, operator=operator)
        return {"column": column, "operator": operator, "value": values}

    if isinstance(value, list):
        raise PlanValidationError(
            "needs_clarification",
            f"Operator {operator!r} requires a single scalar value, not a list.",
        )

    scalar = _scalar_value(value)
    if scalar is None or scalar == "":
        raise PlanValidationError("needs_clarification", f"Operator {operator!r} requires a value.")
    _validate_scalar_lengths([scalar], operator=operator)
    return {"column": column, "operator": operator, "value": scalar}


def _normalize_list_values(value: Any, *, operator: str = "in") -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip()]
        _reject_separator_in_values(values, operator=operator)
        return values
    text = str(value).strip()
    if not text:
        return []
    if catalog.IN_VALUE_SEPARATOR in text:
        return [part.strip() for part in text.split(catalog.IN_VALUE_SEPARATOR) if part.strip()]
    return [text]


def _scalar_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip()


def _reject_separator_in_values(values: list[str], *, operator: str) -> None:
    for item in values:
        if catalog.IN_VALUE_SEPARATOR in item:
            raise PlanValidationError(
                "error",
                f"Condition value for {operator!r} must not contain the separator {catalog.IN_VALUE_SEPARATOR!r}.",
            )


def _validate_scalar_lengths(values: list[str], *, operator: str) -> None:
    for item in values:
        if len(item) > catalog.MAX_CONDITION_VALUE_LENGTH:
            raise PlanValidationError(
                "error",
                f"Condition value for {operator!r} exceeds {catalog.MAX_CONDITION_VALUE_LENGTH} characters.",
            )


def _applied_condition(condition: dict[str, Any]) -> dict[str, Any]:
    applied = {
        "column": condition["column"],
        "operator": condition["operator"],
    }
    if "value" in condition:
        applied["value"] = condition["value"]
    return applied
