"""Tool and catalog schemas for navigator_bi_search."""

from __future__ import annotations

from typing import Any

from . import constants as catalog

NAVIGATOR_BI_SCHEMA = {
    "name": "navigator_bi_search",
    "description": (
        "Query financial and management-reporting data from connected Navigator BI "
        "models. Use for any numeric financial fact the user asks about (PL, income, "
        "balance, credits, deposits, OPEX, KPIs, plans, forecasts) by period, "
        "division, block, or product; never answer such questions from memory. "
        "Call mode=schema without a model first to list the connected models, and "
        "mode=schema with a model when column names are unclear. "
        "Returns human-readable column names, not internal field IDs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["schema", "query"],
                "description": "schema: list models and column metadata. query: run a validated data selection.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Model sName or numeric nID from mode=schema catalog "
                    "(models are loaded from NAVIGATOR_BI_MODEL_IDS). "
                    "Required for query; optional for schema (omit for full catalog)."
                ),
            },
            "plan": {
                "type": "object",
                "description": "Bounded query plan for mode=query.",
                "properties": {
                    "model": {"type": "string"},
                    "conditions": {
                        "type": "array",
                        "minItems": 1,
                        "description": "At least one filter condition is required for a bounded query.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string", "description": "Column sName from schema response."},
                                "operator": {
                                    "type": "string",
                                    "enum": sorted(catalog.ALLOWED_OPERATORS),
                                },
                                "value": {
                                    "description": "Scalar for comparisons; array of strings for in/not in.",
                                },
                            },
                            "required": ["column", "operator"],
                        },
                    },
                    "limit": {
                        "type": "integer",
                        "default": catalog.DEFAULT_ROW_LIMIT,
                        "maximum": catalog.MAX_ROW_LIMIT,
                    },
                },
                "required": ["model"],
            },
            "limit": {
                "type": "integer",
                "default": catalog.DEFAULT_ROW_LIMIT,
                "description": "Legacy top-level alias; plan.limit takes precedence in query mode.",
            },
        },
        "required": ["mode"],
    },
}


def schema_payload(*, models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": "ok",
        "backend": "navigator_bi",
        "tool": "navigator_bi_search",
        "models": models if models is not None else [],
        "allowed_operators": sorted(catalog.ALLOWED_OPERATORS),
        "limits": catalog.limits_payload(),
        "columns": [],
        "plan_notes": [
            "Use only columns listed in columns when model is specified.",
            "At least one filter condition is required; empty conditions are rejected.",
            "Conditions are combined with AND.",
            "Do not invent financial numbers; always query this tool.",
        ],
    }
