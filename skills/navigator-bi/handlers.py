"""Mode handlers for navigator_bi_search."""

from __future__ import annotations

from typing import Any

from . import api, filters, structure_catalog as live_catalog
from .api import NavigatorConfig
from .filters import PlanValidationError
from .schema import schema_payload


def handle_schema(
    settings: NavigatorConfig,
    *,
    model: str = "",
    deadline: api.NavigatorDeadline | None = None,
) -> dict[str, Any]:
    structure_catalog = live_catalog.StructureCatalog.fetch(settings, deadline=deadline)
    payload = schema_payload(models=live_catalog.catalog_entries(structure_catalog))
    model_text = str(model or "").strip()
    if not model_text:
        return payload

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

    payload["columns"] = live_catalog.columns_from_model_node(info.raw)
    return payload


def handle_query(
    settings: NavigatorConfig,
    plan: dict[str, Any] | None,
    *,
    model: str = "",
    limit: int | None = None,
    deadline: api.NavigatorDeadline | None = None,
) -> dict[str, Any]:
    active_plan = dict(plan or {})
    if model and not active_plan.get("model"):
        active_plan["model"] = model
    if "limit" not in active_plan and limit is not None:
        active_plan["limit"] = limit

    structure_catalog = live_catalog.StructureCatalog.fetch(settings, deadline=deadline)
    validated = filters.validate_query_plan(active_plan, structure_catalog=structure_catalog)

    dataset_payload = api.post_dataset(settings, filters.dataset_request_body(validated), deadline=deadline)
    raw_rows, id_to_name = parse_dataset_response(dataset_payload)
    normalized_rows = [normalize_row(row, id_to_name) for row in raw_rows]

    row_limit = validated.limit
    truncated = len(normalized_rows) > row_limit
    page = normalized_rows[:row_limit]

    envelope: dict[str, Any] = {
        "model": validated.model_sname,
        "returned": len(page),
        "limit": row_limit,
        "truncated": truncated,
        "has_more": truncated,
        "applied_conditions": validated.applied_conditions,
        "rows": page,
    }
    if not page:
        return {"status": "not_found", **envelope}
    return {"status": "ok", **envelope}


def _as_dict_list(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        return [node]
    return []


def parse_dataset_response(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    datasets = payload.get("datasets") or {}
    if not isinstance(datasets, dict):
        return [], {}
    dataset_list = _as_dict_list(datasets.get("dataset"))
    if not dataset_list:
        return [], {}
    first = dataset_list[0]

    fields_container = first.get("fields") or {}
    fields = fields_container.get("field") if isinstance(fields_container, dict) else None
    id_to_name: dict[str, str] = {}
    for field in _as_dict_list(fields):
        s_id = field.get("sID")
        s_name = field.get("sName")
        if s_id and s_name:
            id_to_name[str(s_id)] = str(s_name)

    rows_container = first.get("rows") or {}
    row_items = rows_container.get("row") if isinstance(rows_container, dict) else None
    raw_rows = _as_dict_list(row_items)
    return raw_rows, id_to_name


def normalize_row(row: dict[str, Any], id_to_name: dict[str, str]) -> dict[str, Any]:
    return {id_to_name.get(str(key), str(key)): value for key, value in row.items()}
