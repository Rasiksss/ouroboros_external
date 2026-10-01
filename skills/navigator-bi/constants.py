"""Policy limits and wire constants for navigator_bi_search."""

from __future__ import annotations

DEFAULT_BASE_URL = "https://tvlds-mis000010.delta.sbrf.ru:8449"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_TOTAL_TIMEOUT_SEC = 120.0

DEFAULT_ROW_LIMIT = 20
MAX_ROW_LIMIT = 100
MAX_CONDITIONS = 10
MAX_IN_VALUES = 20
MAX_CONDITION_VALUE_LENGTH = 500

STRUCTURE_PATH = "/ai/dataset/structure"
DATASET_PATH = "/ai/dataset"

COMPARISON_OPERATORS = frozenset({"=", "!=", ">", ">=", "<", "<="})
LIST_OPERATORS = frozenset({"in", "not in"})
NULL_OPERATORS = frozenset({"is null", "is not null"})
ALLOWED_OPERATORS = COMPARISON_OPERATORS | LIST_OPERATORS | NULL_OPERATORS

IN_VALUE_SEPARATOR = "|||"


def normalize_model_key(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def limits_payload() -> dict[str, int]:
    return {
        "default_rows": DEFAULT_ROW_LIMIT,
        "max_rows": MAX_ROW_LIMIT,
        "max_conditions": MAX_CONDITIONS,
    }
