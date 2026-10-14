"""Finengine extraction E2E quality harness.

The harness uses the canonical benchmark methodology:

* key = (dataset, document, enum, period, segment)
* statuses = TP / EFP / EFN / EV / TN
* headline = macro-by-document, with micro and row-level diagnostics saved

Real LLM/OpenRouter execution is opt-in via `--allow-llm`; mock/replay modes
are used by unit tests and local debugging without token spend. Segment
breakdown is controlled by `--include-segments` / `--no-segments`.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterable
import unicodedata

import openpyxl

ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_ROOT = ROOT / "tests" / "test_benchmark"
E2E_ROOT = BENCHMARK_ROOT
GOLDEN_ROOT = BENCHMARK_ROOT / "data" / "golden"
DEFAULT_RUNS_DIR = BENCHMARK_ROOT / "runs"
DEFAULT_OPENROUTER_ENV_FALLBACK = ROOT.parent / ".anti-agents.env"
FRONTEND_DEFAULT_MODEL_ID = "qwen/qwen3.6-plus"
NUMERIC_ONLY_UNIT_MISMATCH_SCORE = 0.85

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from finengine.config import settings  # noqa: E402
from finengine.models.document import CompanyType, DocType, DocumentMeta  # noqa: E402
from finengine.skills.metric_registry import (  # noqa: E402
    CALCULATED_BANK,
    CALCULATED_NON_FINANCIAL,
    EXTRACTABLE_BANK,
    EXTRACTABLE_NON_FINANCIAL,
)
from finengine.skills.bulk_extractor import (  # noqa: E402
    _parse_verbatim_value,
    extract_stream_finengine_dev,
)
from finengine.skills.periods import (  # noqa: E402
    POINT_KIND,
    normalize_indian_fiscal_period_label,
    normalize_period_label,
    period_kind_for_enum,
)
from finengine.skills.period_validation import normalize_metric_period  # noqa: E402


class NormalizationMode(StrEnum):
    NUMERIC = "NUMERIC"
    STRING = "STRING"

    @classmethod
    def parse(cls, raw: str | None) -> "NormalizationMode":
        value = (raw or cls.NUMERIC.value).strip().upper()
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Invalid normalization={raw!r}. Allowed: {allowed}") from exc


class UnitMode(StrEnum):
    AUTO = "AUTO"
    IGNORE = "IGNORE"
    REQUIRE = "REQUIRE"

    @classmethod
    def parse(cls, raw: str | None) -> "UnitMode":
        value = (raw or cls.AUTO.value).strip().upper()
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Invalid unit_mode={raw!r}. Allowed: {allowed}") from exc


class SegmentMode(StrEnum):
    CORE = "core"
    EXTENDED = "extended"

    @classmethod
    def parse(cls, raw: str | None) -> "SegmentMode":
        value = (raw or cls.EXTENDED.value).strip().lower().replace("-", "_")
        if value in {"required", "main", "basic"}:
            value = cls.CORE.value
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Invalid segment_mode={raw!r}. Allowed: {allowed}") from exc


class PredictionScope(StrEnum):
    REFERENCE_ENUMS = "REFERENCE_ENUMS"
    # Main (non-segment) predictions are ALWAYS scored — a value returned for
    # an in-scope metric that the (complete) golden GT does not contain is a
    # hallucination and must count as EFP, not be silently ignored. Segment
    # predictions keep the REFERENCE_ENUMS filter because segment GT is
    # intentionally incomplete.
    MAIN_STRICT = "MAIN_STRICT"
    ALL = "ALL"

    @classmethod
    def parse(cls, raw: str | None) -> "PredictionScope":
        value = (raw or cls.REFERENCE_ENUMS.value).strip().upper()
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Invalid prediction_scope={raw!r}. Allowed: {allowed}") from exc


class PeriodKeyMode(StrEnum):
    STRICT = "STRICT"
    FY_31_DEC_EQUIV = "FY_31_DEC_EQUIV"

    @classmethod
    def parse(cls, raw: str | None) -> "PeriodKeyMode":
        value = (raw or cls.STRICT.value).strip().upper()
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Invalid period_key_mode={raw!r}. Allowed: {allowed}") from exc


class GroundTruthKind(StrEnum):
    SEGMENT_ONLY = "segment_only"
    FULL = "full"
    MAIN_OLEG = "main_oleg"
    LEGACY_SEGMENT_ONLY = "legacy_segment_only"

    @classmethod
    def parse(cls, raw: str | None) -> "GroundTruthKind":
        value = (raw or cls.SEGMENT_ONLY.value).strip().lower().replace("-", "_")
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Invalid gt_kind={raw!r}. Allowed: {allowed}") from exc


DATASET_PRESETS = {
    "ifrs_nf_ru": ("IFRS_NF_RU", "ifrs_nf_ru"),
    "us_gaap_nf_eng": ("US_GAAP_NF_EN", "us_gaap_nf_en"),
    "ifrs_bank_ru": ("IFRS_BANK_RU", "ifrs_banks_ru"),
}


def _canon_enum_for_set(enum: str) -> str:
    value = str(enum or "").strip().upper()
    if value == "OPERATING_INCOME":
        return "OPERATING_PROFIT"
    return value


EXTRACTABLE_ENUMS = {
    _canon_enum_for_set(item["enum"])
    for item in [*EXTRACTABLE_NON_FINANCIAL, *EXTRACTABLE_BANK]
}
CALCULATED_ENUMS = {
    _canon_enum_for_set(item[0])
    for item in [*CALCULATED_NON_FINANCIAL, *CALCULATED_BANK]
}
CALCULATED_ONLY_ENUMS = CALCULATED_ENUMS - EXTRACTABLE_ENUMS
US_GAAP_NF_WEAK_METRICS = {"OTHER_ASSETS", "OTHER_LIABILITIES"}
DIE_NF_METRICS = {
    "TOTAL_ASSETS",
    "TOTAL_EQUITY",
    "SALES_REVENUE",
    "OPERATING_PROFIT",
    "COSTS_EXPENSES",
    "NET_INCOME",
    "GROSS_PROFIT",
    "OPERATING_EXPENSES",
    "CAPEX",
    "DIVIDENDS",
    "ASSET_PURCHASES",
    "ASSET_SALES_PROCEEDS",
}
DIE_NF_US_GAAP_METRICS = DIE_NF_METRICS


def _metric_scope_from_registry_json(path: Path, *keys: str) -> set[str]:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        for key in keys:
            data = data[key]
        if isinstance(data, list):
            return {
                _canon_enum_for_set(item.get("enum", ""))
                for item in data
                if isinstance(item, dict) and item.get("enum")
            }
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return set()


US_GAAP_BANK_METRICS = _metric_scope_from_registry_json(
    ROOT / "src" / "finengine" / "data" / "registry" / "us_gaap_bank_en.json",
    "extractable",
) or {_canon_enum_for_set(item["enum"]) for item in EXTRACTABLE_BANK}
try:
    from finengine.roles.scope import build_role_plan

    _DIE_BANK_ROLE_PLAN = build_role_plan("die", "bank", profile_id="ifrs_bank_ru")
    DIE_BANK_ROLE_OUTPUT_METRICS = {
        _canon_enum_for_set(enum)
        for enum in (_DIE_BANK_ROLE_PLAN.output_enums or set())
    }
    DIE_BANK_ROLE_EXTRACTION_METRICS = {
        _canon_enum_for_set(enum)
        for enum in (_DIE_BANK_ROLE_PLAN.extraction_enums or set())
    }
except Exception:
    DIE_BANK_ROLE_OUTPUT_METRICS = set()
    DIE_BANK_ROLE_EXTRACTION_METRICS = set()
DIE_BANK_METRICS = {
    "TOTAL_ASSETS",
    "TOTAL_EQUITY",
    "NET_INCOME",
    "DIVIDENDS",
    "TOTAL_LOANS_TO_CLIENTS",
    "CLIENT_DEPOSITS",
    "COMMON_EQUITY_TIER_ONE_CAPITAL",
    "NET_INTEREST_INCOME",
    "NON_INTEREST_INCOME_BANKS",
    "OPERATING_INCOME_BANKS",
    "TOTAL_CREDIT_ALLOWANCE",
    "OPERATING_EXPENSES_BANKS",
    "TOTAL_INVESTMENT_BANKING_FEES",
    "CHANGE_IN_EXPECTED_CREDIT_LOSSES",
    "INTERBANK_DEPOSITS",
    "SECURITIES_ASSETS",
}
METRIC_SCOPE_PRESETS = {
    "die_nf": DIE_NF_METRICS,
    "die_nf_ifrs": DIE_NF_METRICS,
    "die_nf_ru": DIE_NF_METRICS,
    "die_nf_usgaap": DIE_NF_METRICS,
    "die_nf_us_gaap": DIE_NF_METRICS,
    "us_gaap_bank": US_GAAP_BANK_METRICS,
    "usgaap_bank": US_GAAP_BANK_METRICS,
    "us_gaap_bank_en": US_GAAP_BANK_METRICS,
    "die_bank": DIE_BANK_METRICS,
    "die_bank_usgaap": DIE_BANK_METRICS,
    "die_bank_us_gaap": DIE_BANK_METRICS,
    "die_usgaap_bank": DIE_BANK_METRICS,
    "die_us_gaap_bank": DIE_BANK_METRICS,
    "role_die_bank_output": DIE_BANK_ROLE_OUTPUT_METRICS or DIE_BANK_METRICS,
    "role_die_bank_extraction": DIE_BANK_ROLE_EXTRACTION_METRICS or DIE_BANK_METRICS,
}


def _full_extractable_metric_enums(company_type: str) -> set[str]:
    if company_type == "bank":
        return {_canon_enum(item["enum"]) for item in EXTRACTABLE_BANK}
    return {_canon_enum(item["enum"]) for item in EXTRACTABLE_NON_FINANCIAL}


def _metric_scope_exclusions(
    *,
    company_type: str,
    exclude_us_gaap_nf_weak_metrics: bool = False,
) -> set[str]:
    if company_type == "non_financial" and exclude_us_gaap_nf_weak_metrics:
        return set(US_GAAP_NF_WEAK_METRICS)
    return set()


def _metric_scope_inclusions(metric_scope: str | None) -> set[str] | None:
    raw = str(metric_scope or "").strip()
    value = raw.lower().replace("-", "_")
    if not value or value in {"full", "full_registry", "all"}:
        return None
    for prefix in ("enums:", "enum:", "metrics:", "metric:"):
        if value.startswith(prefix):
            enums = {
                _canon_enum(part)
                for part in raw[len(prefix):].replace(";", ",").split(",")
                if part.strip()
            }
            if not enums:
                raise ValueError(f"Empty metric_scope={metric_scope!r}; expected e.g. enums:TOTAL_ASSETS,NET_INCOME")
            return enums
    if value not in METRIC_SCOPE_PRESETS:
        allowed = ", ".join(["full", "enums:ENUM1,ENUM2", *sorted(METRIC_SCOPE_PRESETS)])
        raise ValueError(f"Unknown metric_scope={metric_scope!r}. Allowed: {allowed}")
    return {_canon_enum(enum) for enum in METRIC_SCOPE_PRESETS[value]}


def _filter_main_rows_by_metric_scope(
    rows: list["SegmentQualityRow"],
    metric_scope_inclusions: set[str] | None,
) -> list["SegmentQualityRow"]:
    """Filter only main-metric GT rows; segment GT is scored by its own segment scope."""
    if not metric_scope_inclusions:
        return rows
    return [
        row for row in rows
        if _canon_segment(row.segment) or _canon_enum(row.enum) in metric_scope_inclusions
    ]


def _profile_id_hint_for_role(dataset_name: str, company_type: str) -> str | None:
    name = str(dataset_name or "").upper()
    company = str(company_type or "").strip().lower()
    if company == "bank":
        return "us_gaap_bank_en" if "US_GAAP" in name else "ifrs_bank_ru"
    if "US_GAAP" in name:
        return "us_gaap_nf_en"
    if "IFRS" in name:
        return "ifrs_nf_ru"
    return None


def _role_metric_sets(
    role: str | None,
    company_type: str,
    *,
    dataset_name: str = "",
) -> tuple[set[str] | None, set[str] | None]:
    normalized = str(role or "").strip()
    if not normalized:
        return None, None
    try:
        from finengine.roles.scope import build_role_plan

        plan = build_role_plan(
            normalized,
            company_type,
            profile_id=_profile_id_hint_for_role(dataset_name, company_type),
        )
    except Exception:
        return None, None
    extraction = (
        {_canon_enum_for_set(enum) for enum in plan.extraction_enums}
        if plan.extraction_enums is not None
        else None
    )
    output = (
        {_canon_enum_for_set(enum) for enum in plan.output_enums}
        if plan.output_enums is not None
        else None
    )
    return extraction, output


def _is_retryable_real_run_error(meta: dict[str, Any]) -> bool:
    message = str(meta.get("error") or "").lower()
    if not message:
        return False
    retry_markers = (
        "peer closed",
        "incomplete chunked",
        "timeout",
        "timed out",
        "connection",
        "temporarily",
        "rate limit",
        "model did not return",
        "empty response",
        "no response",
        "не вернула ответ",
        "502",
        "503",
        "504",
    )
    return any(marker in message for marker in retry_markers)


def is_calculated_enum(enum: str) -> bool:
    """True only for enums that are calculated and not also extractable."""
    return _canon_enum(enum) in CALCULATED_ONLY_ENUMS


def filter_calculated_rows(
    rows: list["SegmentQualityRow"],
) -> tuple[list["SegmentQualityRow"], list["SegmentQualityRow"]]:
    kept: list[SegmentQualityRow] = []
    ignored: list[SegmentQualityRow] = []
    for row in rows:
        if is_calculated_enum(row.enum):
            ignored.append(row)
        else:
            kept.append(row)
    return kept, ignored


IGNORED_SEGMENT_NAMES = {"total", "adjustments"}


def filter_total_segment_rows(
    rows: list["SegmentQualityRow"],
) -> tuple[list["SegmentQualityRow"], list["SegmentQualityRow"]]:
    """Exclude diagnostic/non-business segment rows from quality metrics.

    Empty segment means the main metric row and stays scoreable. Only explicit
    segment breakdown rows labelled TOTAL/ADJUSTMENTS are diagnostic context or
    reconciliation rows, not quality targets.
    """
    kept: list[SegmentQualityRow] = []
    ignored: list[SegmentQualityRow] = []
    for row in rows:
        if _canon_segment(row.segment) in IGNORED_SEGMENT_NAMES:
            ignored.append(row)
        else:
            kept.append(row)
    return kept, ignored


def _canon_segment_type(value: Any) -> str:
    raw = _normalize_header(value)
    if raw in {"required", "core", "main", "основной", "обязательный"}:
        return "required"
    if raw in {"optional", "cross", "cross breakdown", "перекрестный", "дополнительный"}:
        return "optional"
    return ""


def filter_optional_segment_rows(
    rows: list["SegmentQualityRow"],
) -> tuple[list["SegmentQualityRow"], list["SegmentQualityRow"]]:
    kept: list[SegmentQualityRow] = []
    ignored: list[SegmentQualityRow] = []
    for row in rows:
        if _canon_segment(row.segment) and _canon_segment_type(row.segment_type) == "optional":
            ignored.append(row)
        else:
            kept.append(row)
    return kept, ignored


def _segment_scope_rows(
    rows: list["SegmentQualityRow"],
    *,
    include_optional: bool,
) -> list["SegmentQualityRow"]:
    out: list[SegmentQualityRow] = []
    for row in rows:
        if not _canon_segment(row.segment):
            continue
        if not include_optional and _canon_segment_type(row.segment_type) == "optional":
            continue
        out.append(row)
    return out


NULL_TOKEN = "null"
NULL_STRINGS = {
    "",
    "-",
    "—",
    "none",
    "null",
    "nan",
    "na",
    "n/a",
    "нет",
    "нет данных",
    "not found",
    "missing",
}

UNSCORED_REFERENCE_STRINGS = {
    "calculated",
    "рассчитываемые",
}


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_openrouter_key_fallback(
    fallback_path: Path | None = DEFAULT_OPENROUTER_ENV_FALLBACK,
    *,
    prefer_fallback: bool = False,
) -> dict[str, str | bool]:
    """Fill OPENROUTER_API_KEY from a sibling env file for E2E tests.

    Precedence stays conservative: already-loaded settings win, then the
    process environment, then `../.anti-agents.env`. The fallback deliberately
    does not load model/provider settings, so a shared token file cannot
    silently change benchmark model selection.
    """
    if prefer_fallback and fallback_path is not None:
        values = _read_env_file(fallback_path)
        token = values.get("OPENROUTER_API_KEY", "").strip()
        if token:
            os.environ["OPENROUTER_API_KEY"] = token
            settings.openrouter_api_key = token
            return {"loaded": True, "source": "fallback_override", "path": str(fallback_path)}

    if settings.openrouter_api_key:
        return {"loaded": False, "source": "settings", "path": ""}

    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        settings.openrouter_api_key = env_key
        return {"loaded": False, "source": "environment", "path": ""}

    if fallback_path is None:
        return {"loaded": False, "source": "missing", "path": ""}

    values = _read_env_file(fallback_path)
    token = values.get("OPENROUTER_API_KEY", "").strip()
    if not token:
        return {"loaded": False, "source": "missing", "path": str(fallback_path)}

    os.environ["OPENROUTER_API_KEY"] = token
    settings.openrouter_api_key = token
    return {"loaded": True, "source": "fallback", "path": str(fallback_path)}


@dataclass(frozen=True)
class SegmentQualityRow:
    dataset: str
    document: str
    enum: str
    period: str
    segment: str
    value: str
    unit: str = ""
    segment_type: str = ""
    source: str = ""
    row_id: int | None = None

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.dataset,
            _canon_pdf(self.document),
            _canon_enum(self.enum),
            normalize_metric_period(self.period, self.enum).period,
            _canon_segment(self.segment),
        )

    @property
    def key_str(self) -> str:
        return "|".join(self.key)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"key": self.key_str}


@dataclass
class EvalCounts:
    TP: float = 0.0
    EFP: int = 0
    EFN: int = 0
    EV: int = 0
    TN: int = 0
    duplicates: int = 0
    business_tp_sum: float = 0.0
    pred_positive_count: int = 0
    ref_positive_count: int = 0
    tp_like_slots: int = 0

    def add(self, other: "EvalCounts") -> None:
        for field_name in self.__dataclass_fields__:
            setattr(self, field_name, getattr(self, field_name) + getattr(other, field_name))


@dataclass
class MetricValues:
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    f05: float = 0.0
    accuracy: float = 0.0
    business_precision: float = 0.0
    business_recall: float = 0.0
    business_f1: float = 0.0
    business_f05: float = 0.0
    business_utility: float = 0.0


@dataclass
class EvalMetrics:
    scope: str
    counts: EvalCounts = field(default_factory=EvalCounts)
    values: MetricValues = field(default_factory=MetricValues)

    def to_flat_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, **asdict(self.counts), **asdict(self.values)}


@dataclass
class RowComparison:
    dataset: str
    document: str
    enum: str
    period: str
    segment: str
    key: str
    status: str
    reasons: str
    ref_value: str
    pred_value: str
    ref_value_norm: str
    pred_value_norm: str
    ref_unit: str
    pred_unit: str
    business_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationResult:
    by_document: dict[str, EvalMetrics]
    by_enum: dict[str, EvalMetrics]
    micro: EvalMetrics
    macro_by_document: EvalMetrics
    row_comparisons: list[RowComparison]


@dataclass
class SegmentQualityRun:
    run_dir: Path
    result: EvaluationResult
    reference_rows: list[SegmentQualityRow]
    prediction_rows: list[SegmentQualityRow]
    files: dict[str, Path]
    run_records: list[dict[str, Any]]
    llm_output_rows: list[dict[str, Any]] = field(default_factory=list)
    overall_core_result: EvaluationResult | None = None
    overall_all_result: EvaluationResult | None = None
    main_result: EvaluationResult | None = None
    result_without_unit: EvaluationResult | None = None
    overall_core_result_without_unit: EvaluationResult | None = None
    overall_all_result_without_unit: EvaluationResult | None = None
    segment_core_result: EvaluationResult | None = None
    segment_all_result: EvaluationResult | None = None
    segment_core_result_without_unit: EvaluationResult | None = None
    segment_all_result_without_unit: EvaluationResult | None = None


@dataclass(frozen=True)
class DatasetInput:
    name: str
    gt_xlsx: Path
    json_paths: tuple[Path, ...]
    root_dir: Path | None = None
    gt_sheet: str | None = None
    input_mode: str = "pdf"


OCR_JSON_VARIANTS = {"old", "new", "both"}


def nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _canon_pdf(name: str) -> str:
    filename = nfc(Path(str(name).replace("\\", "/")).name)
    filename = " ".join(filename.replace("\u00a0", " ").split())
    return filename.lower()


def _doc_match_key(name: str) -> str:
    """Strict-ish file stem key used only to pair GT document names with inputs."""
    filename = re.sub(r"\.[Pp][Dd][Ff]$", "", _canon_pdf(name))
    return re.sub(r"[^0-9a-zа-я._-]+", "", filename)


def _canon_enum(enum: str) -> str:
    value = str(enum or "").strip().upper()
    if value == "OPERATING_INCOME":
        return "OPERATING_PROFIT"
    return value


def _normalize_ocr_json_variant(value: str | None) -> str:
    normalized = str(value or "old").strip().lower().replace("-", "_")
    if normalized not in OCR_JSON_VARIANTS:
        raise ValueError(f"--ocr-json-variant must be one of: {', '.join(sorted(OCR_JSON_VARIANTS))}")
    return normalized


def _is_new_ocr_json(path: Path) -> bool:
    return path.stem.lower().endswith("_new")


def _filter_ocr_json_variant(paths: Iterable[Path], variant: str) -> tuple[Path, ...]:
    normalized = _normalize_ocr_json_variant(variant)
    if normalized == "both":
        return tuple(paths)
    want_new = normalized == "new"
    return tuple(path for path in paths if _is_new_ocr_json(path) == want_new)


def _canon_segment(segment: str) -> str:
    value = nfc(segment).strip().lower().replace("ё", "е")
    value = " ".join(value.split())
    if value in {"total", "segment total", "total segment"}:
        return "total"
    return value


def _canon_unit(unit: str) -> str:
    value = nfc(unit).lower().replace("\u00a0", " ").replace("ё", "е")
    if "%" in value:
        return "percent"
    value = value.replace("$", " usd ")
    value = value.replace("\u20b9", " inr ")
    normalized = re.sub(r"[^a-zа-я0-9]+", " ", value)
    tokens = set(normalized.split())
    compact = "".join(normalized.split())
    if not compact:
        return ""
    if tokens & {"percent", "percentage", "процент", "процента", "процентов"}:
        return "percent"

    currency = ""
    if tokens & {"rub", "rur", "руб", "рубли", "рублей", "рубль"} or "руб" in compact:
        currency = "RUB"
    elif tokens & {"usd", "us", "dollar", "dollars", "долл", "доллар", "долларов"}:
        currency = "USD"
    elif tokens & {"inr", "rs", "rupee", "rupees"} or "rupee" in compact:
        currency = "INR"
    elif tokens & {"eur", "euro", "euros"}:
        currency = "EUR"

    scale = ""
    if (
        tokens & {"crore", "crores", "cr"}
        or "crore" in compact
    ):
        scale = "crore"
    elif (
        tokens & {"lakh", "lakhs", "lac", "lacs"}
        or "lakh" in compact
    ):
        scale = "lakh"
    elif (
        tokens & {"arab", "arabs"}
        or "arab" in compact
    ):
        scale = "arab"
    elif (
        tokens & {"million", "millions", "mln", "m", "млн", "миллион", "миллиона", "миллионов"}
        or "million" in compact
        or "млн" in compact
    ):
        scale = "million"
    elif (
        tokens & {"thousand", "thousands", "k", "тыс", "тысяча", "тысяч"}
        or "thousand" in compact
        or "тыс" in compact
    ):
        scale = "thousand"
    elif (
        tokens & {"billion", "billions", "bn", "b", "млрд", "миллиард", "миллиарда", "миллиардов"}
        or "billion" in compact
        or "млрд" in compact
    ):
        scale = "billion"

    per_share = (
        bool(tokens & {"share", "shares", "per", "акция", "акцию", "акции", "акц"})
        or "pershare" in compact
        or "наакц" in compact
    )
    if currency or scale or per_share:
        return ":".join(part for part in (currency, scale, "share" if per_share else "") if part)
    return compact


def _unit_parts(unit: str) -> tuple[str, str, bool]:
    parts = _canon_unit(unit).split(":")
    currency = next((part for part in parts if part in {"RUB", "USD", "INR", "EUR"}), "")
    scale = next((part for part in parts if part in {"thousand", "lakh", "million", "crore", "arab", "billion"}), "")
    return currency, scale, "share" in parts


def _unit_scale(unit: str) -> float:
    scale = _unit_parts(unit)[1]
    if scale == "thousand":
        return 1_000.0
    if scale == "lakh":
        return 100_000.0
    if scale == "million":
        return 1_000_000.0
    if scale == "crore":
        return 10_000_000.0
    if scale == "arab":
        return 1_000_000_000.0
    if scale == "billion":
        return 1_000_000_000.0
    return 1.0


def _display_unit_for_enum(enum: str, unit: str) -> str:
    if _canon_enum(enum) != "EPS":
        return str(unit or "")
    value = nfc(unit).lower().replace("\u00a0", " ").replace("ё", "е")
    if "коп" in value or "kopeck" in value or "kopey" in value:
        return "коп./акция"
    canon = _canon_unit(value)
    if "USD" in canon:
        return "$/share"
    if "RUB" in canon or "руб" in value or "₽" in value:
        return "руб/акция"
    return str(unit or "") or "руб/акция"


def _units_compatible(ref_unit: str, pred_unit: str) -> bool:
    ref_canon = _canon_unit(ref_unit)
    pred_canon = _canon_unit(pred_unit)
    if ref_canon == pred_canon:
        return True
    if ref_canon == "percent" or pred_canon == "percent":
        return False
    ref_currency, _ref_scale, ref_share = _unit_parts(ref_unit)
    pred_currency, _pred_scale, pred_share = _unit_parts(pred_unit)
    if ref_share != pred_share:
        return False
    ref_has_money = bool(ref_currency or _ref_scale)
    pred_has_money = bool(pred_currency or _pred_scale)
    if ref_has_money != pred_has_money:
        return False
    if not ref_has_money:
        return False
    if ref_currency and pred_currency and ref_currency != pred_currency:
        return False
    return True


def _units_equivalent_for_enum(enum: str, ref_unit: str, pred_unit: str) -> bool:
    if _canon_enum(enum) != "EPS":
        return False
    if not str(ref_unit or "").strip() or not str(pred_unit or "").strip():
        return False
    ref_currency, ref_scale, _ref_share = _unit_parts(ref_unit)
    pred_currency, pred_scale, _pred_share = _unit_parts(pred_unit)
    if ref_scale or pred_scale:
        return False
    return bool(ref_currency and pred_currency and ref_currency == pred_currency)


def _period_key(period: str, mode: PeriodKeyMode | str = PeriodKeyMode.STRICT) -> str:
    normalized = normalize_period_label(period)
    parsed_mode = mode if isinstance(mode, PeriodKeyMode) else PeriodKeyMode.parse(str(mode))
    if parsed_mode == PeriodKeyMode.FY_31_DEC_EQUIV:
        m = re.fullmatch(r"31[.]12[.](20\d{2})", normalized)
        if m:
            return f"FY {m.group(1)}"
        m = re.fullmatch(r"12M 31[.]12[.](20\d{2})", normalized)
        if m:
            return f"FY {m.group(1)}"
    return normalized


def _period_key_for_enum(
    enum: str,
    period: str,
    mode: PeriodKeyMode | str = PeriodKeyMode.STRICT,
) -> str:
    normalized = _period_key(normalize_metric_period(period, enum).period, mode)
    if period_kind_for_enum(_canon_enum(enum)) != POINT_KIND:
        return normalized
    m = re.fullmatch(r"12M (\d{2}[.]\d{2}[.]20\d{2})", normalized)
    if m:
        return m.group(1)
    m = re.fullmatch(r"FY (20\d{2})", normalized)
    if m:
        return f"31.12.{m.group(1)}"
    return normalized


def _row_key(
    row: SegmentQualityRow,
    period_key_mode: PeriodKeyMode | str = PeriodKeyMode.STRICT,
) -> tuple[str, str, str, str, str]:
    return (
        row.dataset,
        _canon_pdf(row.document),
        _canon_enum(row.enum),
        _period_key_for_enum(row.enum, row.period, period_key_mode),
        _canon_segment(row.segment),
    )


def _key_str(key: tuple[str, str, str, str, str]) -> str:
    return "|".join(key)


def _is_null_like(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).replace("\u00a0", " ").strip()
    return text.lower() in NULL_STRINGS


def _canonical_value(value: Any) -> str:
    if _is_null_like(value):
        return NULL_TOKEN
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\u00a0", " ").strip()


def _canonical_reference_value(value: Any) -> str:
    """Parse GT accounting notation while leaving source markup files untouched."""
    if _is_null_like(value):
        return NULL_TOKEN
    parsed = _num(value)
    if parsed is None:
        return _canonical_value(value)
    if abs(parsed - round(parsed)) < 1e-9:
        return str(int(round(parsed)))
    return f"{parsed:.12g}"


def _value_with_source_sign(value: Any, source_text: Any) -> Any:
    """Preserve normalized value scale, but recover accounting sign from source text.

    Older cached extraction events may store ``period_values.value`` after
    pre-sign-normalization while keeping the verbatim cell in ``source_text``.
    Use the source only to detect brackets/minus; do not replace the magnitude,
    because normalized values may already include unit scaling.
    """
    parsed_value = _num(value)
    parsed_source = _num(source_text)
    if parsed_value is None or parsed_source is None:
        return value
    if parsed_source < 0 and parsed_value > 0:
        return -parsed_value
    return value


def _num(value: Any) -> float | None:
    if _is_null_like(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_verbatim_value(str(value))


def _normalize_for_compare(value: Any, mode: NormalizationMode, *, ref_side: bool) -> str:
    if mode == NormalizationMode.STRING:
        return _canonical_value(value)
    parsed = _num(value)
    if parsed is None:
        if ref_side:
            return NULL_TOKEN
        return _canonical_value(value)
    if abs(parsed - round(parsed)) < 1e-9:
        return str(int(round(parsed)))
    return f"{parsed:.12g}"


def _positive_value(value: Any, mode: NormalizationMode, *, ref_side: bool) -> bool:
    return _normalize_for_compare(value, mode, ref_side=ref_side) != NULL_TOKEN


def _values_equal(
    ref_value: Any,
    pred_value: Any,
    mode: NormalizationMode,
    tolerance: float,
    *,
    ref_unit: str = "",
    pred_unit: str = "",
) -> bool:
    ref_norm = _normalize_for_compare(ref_value, mode, ref_side=True)
    pred_norm = _normalize_for_compare(pred_value, mode, ref_side=False)
    if ref_norm == NULL_TOKEN and pred_norm == NULL_TOKEN:
        return True
    if mode == NormalizationMode.STRING:
        return ref_norm == pred_norm
    ref_num = _num(ref_value)
    pred_num = _num(pred_value)
    if ref_num is None or pred_num is None:
        return ref_norm == pred_norm
    if _unit_parts(ref_unit)[1] and _unit_parts(pred_unit)[1] and _units_compatible(ref_unit, pred_unit):
        ref_num *= _unit_scale(ref_unit)
        pred_num *= _unit_scale(pred_unit)
    if abs(ref_num) < 1e-12:
        return abs(pred_num) < 1e-12
    return abs(pred_num - ref_num) / abs(ref_num) <= tolerance


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _f_beta(precision: float, recall: float, beta: float) -> float:
    beta2 = beta * beta
    den = beta2 * precision + recall
    return (1 + beta2) * precision * recall / den if den else 0.0


def _metrics_from_counts(counts: EvalCounts, scope: str) -> EvalMetrics:
    precision = _safe_div(counts.TP, counts.TP + counts.EFP + counts.EV)
    recall = _safe_div(counts.TP, counts.TP + counts.EFN + counts.EV)
    business_precision = _safe_div(counts.business_tp_sum, counts.pred_positive_count)
    business_recall = _safe_div(counts.business_tp_sum, counts.ref_positive_count)
    business_utility = _safe_div(counts.business_tp_sum, counts.tp_like_slots + counts.EFP + counts.EFN)
    return EvalMetrics(
        scope=scope,
        counts=counts,
        values=MetricValues(
            precision=precision,
            recall=recall,
            f1=_f_beta(precision, recall, beta=1.0),
            f05=_f_beta(precision, recall, beta=0.5),
            accuracy=_safe_div(counts.TP, counts.TP + counts.EFP + counts.EFN + counts.EV),
            business_precision=business_precision,
            business_recall=business_recall,
            business_f1=_f_beta(business_precision, business_recall, beta=1.0),
            business_f05=_f_beta(business_precision, business_recall, beta=0.5),
            business_utility=business_utility,
        ),
    )


def _macro_metrics(by_document: dict[str, EvalMetrics]) -> EvalMetrics:
    if not by_document:
        return EvalMetrics(scope="[TOTAL]")
    counts = EvalCounts()
    for item in by_document.values():
        counts.add(item.counts)
    values = MetricValues()
    for field_name in values.__dataclass_fields__:
        setattr(
            values,
            field_name,
            sum(getattr(item.values, field_name) for item in by_document.values()) / len(by_document),
        )
    return EvalMetrics(scope="[TOTAL]", counts=counts, values=values)


def _group_by_key(
    rows: Iterable[SegmentQualityRow],
    period_key_mode: PeriodKeyMode | str = PeriodKeyMode.STRICT,
) -> dict[tuple, list[SegmentQualityRow]]:
    grouped: dict[tuple, list[SegmentQualityRow]] = {}
    for row in rows:
        grouped.setdefault(_row_key(row, period_key_mode), []).append(row)
    return grouped


def _dedupe_scale_equivalent_reference_rows(
    rows: list[SegmentQualityRow],
) -> tuple[SegmentQualityRow | None, int]:
    if not rows:
        return None, 0
    nums = [_num(row.value) for row in rows]
    if any(value is None for value in nums):
        return None, 0
    units = {str(row.unit).strip() for row in rows}
    if len(units) > 1 or next(iter(units), ""):
        return None, 0
    abs_nums = sorted(abs(float(value)) for value in nums if value is not None)
    if not abs_nums:
        return None, 0
    max_abs = abs_nums[-1]
    if max_abs < 1e-12:
        return rows[0], len(rows) - 1
    allowed = {1.0, 1_000.0, 1_000_000.0}
    for value in abs_nums:
        ratio = max_abs / value if value else max_abs
        if not any(abs(ratio - factor) <= max(factor * 1e-9, 1e-9) for factor in allowed):
            return None, 0
    return max(rows, key=lambda row: abs(float(_num(row.value) or 0.0))), len(rows) - 1


def _dedupe_unit_equivalent_reference_rows(
    rows: list[SegmentQualityRow],
    mode: NormalizationMode,
) -> tuple[SegmentQualityRow | None, int]:
    """The same number under compatible units ("RUB billion" vs "млрд руб") is a
    duplicate, not a conflict — the raw unit strings differ but denote the same
    quantity. Collapse it, preferring a row with a non-empty unit. Tight
    tolerance: only exactly-equal magnitudes merge, never genuinely different
    values (gross/net, sign flips stay conflicts)."""
    if len(rows) < 2:
        return None, 0
    first = rows[0]
    for row in rows[1:]:
        if not _values_equal(
            first.value, row.value, mode, 1e-9, ref_unit=first.unit, pred_unit=row.unit
        ):
            return None, 0
    winner = max(rows, key=lambda r: (int(bool(str(r.unit).strip())), -(r.row_id or 0)))
    return winner, len(rows) - 1


def _dedupe_reference_rows(
    rows: list[SegmentQualityRow],
    mode: NormalizationMode,
    period_key_mode: PeriodKeyMode | str = PeriodKeyMode.STRICT,
) -> tuple[list[SegmentQualityRow], int]:
    grouped = _group_by_key(rows, period_key_mode)
    deduped: list[SegmentQualityRow] = []
    duplicate_count = 0
    conflicts: list[tuple] = []
    for key, key_rows in grouped.items():
        if len(key_rows) == 1:
            deduped.append(key_rows[0])
            continue
        value_units = {
            (_normalize_for_compare(row.value, mode, ref_side=True), str(row.unit).strip())
            for row in key_rows
        }
        if len(value_units) > 1:
            scale_row, scale_duplicates = _dedupe_scale_equivalent_reference_rows(key_rows)
            if scale_row is not None:
                duplicate_count += scale_duplicates
                deduped.append(scale_row)
                continue
            unit_row, unit_duplicates = _dedupe_unit_equivalent_reference_rows(key_rows, mode)
            if unit_row is not None:
                duplicate_count += unit_duplicates
                deduped.append(unit_row)
                continue
            conflicts.append(key)
            continue
        duplicate_count += len(key_rows) - 1
        deduped.append(key_rows[0])
    if conflicts:
        preview = "; ".join("|".join(map(str, key)) for key in conflicts[:10])
        raise ValueError(f"Reference dataset contains conflicting duplicate keys: {preview}")
    return deduped, duplicate_count


def _select_primary_prediction(
    rows: list[SegmentQualityRow],
    mode: NormalizationMode,
) -> tuple[SegmentQualityRow | None, list[SegmentQualityRow]]:
    if not rows:
        return None, []

    def score(row: SegmentQualityRow) -> tuple[int, int, int]:
        return (
            int(_positive_value(row.value, mode, ref_side=False)),
            int(bool(str(row.unit).strip())),
            -int(row.row_id or 0),
        )

    best = max(rows, key=score)
    return best, [row for row in rows if row is not best]


def _empty_row_from_key(key: tuple[str, str, str, str, str], *, source: str) -> SegmentQualityRow:
    dataset, document, enum, period, segment = key
    return SegmentQualityRow(
        dataset=dataset,
        document=document,
        enum=enum,
        period=period,
        segment=segment,
        value=NULL_TOKEN,
        segment_type="",
        source=source,
    )


def _compare_pair(
    ref: SegmentQualityRow,
    pred: SegmentQualityRow,
    *,
    mode: NormalizationMode,
    tolerance: float,
    business_alpha: float,
    require_unit: bool,
    comparison_key: str | None = None,
) -> tuple[EvalCounts, RowComparison]:
    counts = EvalCounts()
    reasons: list[str] = []
    ref_positive = _positive_value(ref.value, mode, ref_side=True)
    pred_positive = _positive_value(pred.value, mode, ref_side=False)
    if ref_positive:
        counts.ref_positive_count += 1
    if pred_positive:
        counts.pred_positive_count += 1

    ref_norm = _normalize_for_compare(ref.value, mode, ref_side=True)
    pred_norm = _normalize_for_compare(pred.value, mode, ref_side=False)
    value_match = _values_equal(
        ref.value,
        pred.value,
        mode,
        tolerance,
        ref_unit=ref.unit,
        pred_unit=pred.unit,
    )
    numeric_value_match = _values_equal(ref.value, pred.value, mode, tolerance)
    unit_match = (
        ref.enum == "MODIFIED_AUDITORS_OPINION"
        or (not require_unit)
        or _canon_unit(ref.unit) == _canon_unit(pred.unit)
        or _units_equivalent_for_enum(ref.enum, ref.unit, pred.unit)
        or (value_match and _units_compatible(ref.unit, pred.unit))
    )
    business_score = 0.0

    if ref_positive and pred_positive:
        counts.tp_like_slots += 1
        if value_match and unit_match:
            status = "TP"
            counts.TP += 1.0
            business_score = 1.0
        elif numeric_value_match:
            status = "TP"
            counts.TP += business_alpha
            reasons.append("unit_mismatch_partial_credit")
            business_score = business_alpha
        else:
            status = "EV"
            counts.EV += 1
            if not value_match:
                reasons.append("value_mismatch")
            if value_match and not unit_match:
                reasons.append("unit_mismatch")
                business_score = business_alpha
    elif not ref_positive and pred_positive:
        status = "EFP"
        counts.EFP += 1
        reasons.append("extra_prediction")
    elif ref_positive and not pred_positive:
        status = "EFN"
        counts.EFN += 1
        reasons.append("missing_prediction")
    else:
        status = "TN"
        counts.TN += 1

    counts.business_tp_sum += business_score
    visible = ref if ref.source != "synthetic" else pred
    return counts, RowComparison(
        dataset=visible.dataset,
        document=visible.document,
        enum=visible.enum,
        period=visible.period,
        segment=visible.segment,
        key=comparison_key or visible.key_str,
        status=status,
        reasons=",".join(reasons),
        ref_value=ref.value,
        pred_value=pred.value,
        ref_value_norm=ref_norm,
        pred_value_norm=pred_norm,
        ref_unit=ref.unit,
        pred_unit=pred.unit,
        business_score=business_score,
    )


def evaluate_rows(
    reference_rows: list[SegmentQualityRow],
    prediction_rows: list[SegmentQualityRow],
    *,
    normalization_mode: NormalizationMode | str = NormalizationMode.NUMERIC,
    tolerance: float = 0.05,
    business_alpha: float = NUMERIC_ONLY_UNIT_MISMATCH_SCORE,
    require_unit: bool = False,
    period_key_mode: PeriodKeyMode | str = PeriodKeyMode.STRICT,
) -> EvaluationResult:
    mode = (
        normalization_mode
        if isinstance(normalization_mode, NormalizationMode)
        else NormalizationMode.parse(str(normalization_mode))
    )
    key_mode = (
        period_key_mode
        if isinstance(period_key_mode, PeriodKeyMode)
        else PeriodKeyMode.parse(str(period_key_mode))
    )
    reference_rows, reference_duplicates = _dedupe_reference_rows(reference_rows, mode, key_mode)
    ref_by_key = _group_by_key(reference_rows, key_mode)
    pred_by_key = _group_by_key(prediction_rows, key_mode)

    row_comparisons: list[RowComparison] = []
    counts_by_document: dict[str, EvalCounts] = {}
    counts_by_enum: dict[str, EvalCounts] = {}
    micro_counts = EvalCounts()

    for key in sorted(set(ref_by_key) | set(pred_by_key), key=lambda item: tuple(map(str, item))):
        ref = ref_by_key.get(key, [_empty_row_from_key(key, source="synthetic")])[0]
        primary_pred, duplicates = _select_primary_prediction(pred_by_key.get(key, []), mode)
        pred = primary_pred or _empty_row_from_key(key, source="synthetic")
        pair_counts, comparison = _compare_pair(
            ref,
            pred,
            mode=mode,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=require_unit,
            comparison_key=_key_str(key),
        )
        row_comparisons.append(comparison)
        micro_counts.add(pair_counts)
        counts_by_document.setdefault(comparison.document, EvalCounts()).add(pair_counts)
        counts_by_enum.setdefault(comparison.enum, EvalCounts()).add(pair_counts)

        for duplicate in duplicates:
            dup_positive = _positive_value(duplicate.value, mode, ref_side=False)
            dup_counts = EvalCounts(duplicates=1)
            if dup_positive:
                dup_counts.EFP = 1
                dup_counts.pred_positive_count = 1
            dup_comparison = RowComparison(
                dataset=duplicate.dataset,
                document=duplicate.document,
                enum=duplicate.enum,
                period=duplicate.period,
                segment=duplicate.segment,
                key=duplicate.key_str,
                status="EFP" if dup_positive else "DUPLICATE_NULL",
                reasons="duplicate_prediction",
                ref_value=NULL_TOKEN,
                pred_value=duplicate.value,
                ref_value_norm=NULL_TOKEN,
                pred_value_norm=_normalize_for_compare(duplicate.value, mode, ref_side=False),
                ref_unit="",
                pred_unit=duplicate.unit,
                business_score=0.0,
            )
            row_comparisons.append(dup_comparison)
            micro_counts.add(dup_counts)
            counts_by_document.setdefault(duplicate.document, EvalCounts()).add(dup_counts)
            counts_by_enum.setdefault(duplicate.enum, EvalCounts()).add(dup_counts)

    micro_counts.duplicates += reference_duplicates
    by_document = {
        scope: _metrics_from_counts(counts, scope)
        for scope, counts in sorted(counts_by_document.items())
    }
    by_enum = {
        scope: _metrics_from_counts(counts, scope)
        for scope, counts in sorted(counts_by_enum.items())
    }
    return EvaluationResult(
        by_document=by_document,
        by_enum=by_enum,
        micro=_metrics_from_counts(micro_counts, "[TOTAL_MICRO]"),
        macro_by_document=_macro_metrics(by_document),
        row_comparisons=row_comparisons,
    )


def _normalize_header(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _header_index(headers: Iterable[Any], names: set[str]) -> int | None:
    normalized = {
        _normalize_header(header): idx
        for idx, header in enumerate(headers)
    }
    for name in names:
        idx = normalized.get(_normalize_header(name))
        if idx is not None:
            return idx
    return None


def _required_header_index(headers: Iterable[Any], names: set[str], label: str, path: Path) -> int:
    idx = _header_index(headers, names)
    if idx is None:
        expected = ", ".join(sorted(names))
        raise ValueError(f"{path} has no {label} column. Expected one of: {expected}")
    return idx


def _iter_markup_rows(path: Path, sheet_name: str | None) -> tuple[tuple[Any, ...], list[tuple[int, tuple[Any, ...]]]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            sample = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = [tuple(row) for row in csv.reader(f, dialect)]
        headers = rows[0] if rows else ()
        return headers, [(idx, row) for idx, row in enumerate(rows[1:], start=2)]

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if sheet_name:
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"{path} has no sheet {sheet_name!r}. Sheets: {', '.join(wb.sheetnames)}")
        ws = wb[sheet_name]
    else:
        ws = wb["Лист3"] if "Лист3" in wb.sheetnames else wb[wb.sheetnames[0]]
    headers = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    return tuple(headers), [
        (idx, tuple(row))
        for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2)
    ]


def load_reference_rows(
    gt_xlsx: Path,
    dataset: str,
    *,
    pdf_filter: set[str] | None = None,
    include_segments: bool = True,
    sheet_name: str | None = None,
    legacy_replay: bool = False,
    segment_mode: SegmentMode | str = SegmentMode.EXTENDED,
) -> list[SegmentQualityRow]:
    segment_mode = SegmentMode.parse(str(segment_mode))
    headers, data_rows = _iter_markup_rows(gt_xlsx, sheet_name)
    document_idx = _required_header_index(
        headers,
        {
            "pdf name",
            "pdf",
            "document",
            "filename",
            "file",
            "filepath",
            "file path",
            "path",
            "doc name",
            "report name",
            "файл",
            "путь",
            "путь к файлу",
            "отчет",
            "документ",
        },
        "document/pdf",
        gt_xlsx,
    )
    enum_idx = _required_header_index(
        headers,
        {"enum", "metric enum", "metricenum", "parameter enum", "показатель enum", "метрика"},
        "enum",
        gt_xlsx,
    )
    period_idx = _required_header_index(
        headers,
        {"period", "date", "parameter date", "период", "дата"},
        "period",
        gt_xlsx,
    )
    value_idx = _required_header_index(
        headers,
        {"value", "parameter value", "metric value", "значение", "amount"},
        "value",
        gt_xlsx,
    )
    segment_idx = _header_index(
        headers,
        {"segment", "segment name", "segment label", "сегмент", "название сегмента"},
    )
    segment_type_idx = _header_index(
        headers,
        {"segment type", "segment kind", "segment scope", "тип сегмента", "вид сегмента"},
    )
    unit_idx = _header_index(headers, {"unit", "units", "единица", "единица измерения", "ед. изм"})
    normalized_filter = {_canon_pdf(item) for item in pdf_filter} if pdf_filter else None
    rows: list[SegmentQualityRow] = []
    indian_calendar = _dataset_uses_indian_fiscal_calendar(dataset)
    for idx, row in data_rows:
        if not row or len(row) <= max(document_idx, enum_idx, period_idx, value_idx):
            continue
        if not row[document_idx]:
            continue
        document = str(row[document_idx])
        if normalized_filter and _canon_pdf(document) not in normalized_filter:
            continue
        raw_value = row[value_idx] if len(row) > value_idx else None
        if nfc(raw_value).strip().lower() in UNSCORED_REFERENCE_STRINGS:
            continue
        segment = str(row[segment_idx] or "") if segment_idx is not None and len(row) > segment_idx else ""
        if not include_segments:
            if segment and _canon_segment(segment) != "total":
                continue
            segment = ""
        segment_type = (
            _canon_segment_type(row[segment_type_idx])
            if segment_type_idx is not None and len(row) > segment_type_idx
            else ""
        )
        if include_segments and segment_mode == SegmentMode.CORE and segment_type == "optional":
            continue
        enum = _canon_enum(str(row[enum_idx] or ""))
        raw_period = row[period_idx]
        if indian_calendar:
            period = normalize_indian_fiscal_period_label(
                raw_period,
                value_kind=period_kind_for_enum(enum),
                already_normalized=_looks_like_normalized_period_label(raw_period),
            )
            period = normalize_metric_period(period or raw_period, enum).period
        else:
            period = normalize_metric_period(raw_period, enum).period
        rows.append(
            SegmentQualityRow(
                dataset=dataset,
                document=document,
                enum=enum,
                period=period,
                segment=segment,
                value=(
                    _canonical_value(raw_value)
                    if legacy_replay
                    else _canonical_reference_value(raw_value)
                ),
                unit=str(row[unit_idx] or "") if unit_idx is not None and len(row) > unit_idx else "",
                segment_type=segment_type,
                source="ref",
                row_id=idx,
            )
        )
    return rows


def _resolve_require_unit(
    reference_rows: list[SegmentQualityRow],
    unit_mode: UnitMode | str,
    legacy_require_unit: bool,
) -> bool:
    if legacy_require_unit:
        return True
    mode = unit_mode if isinstance(unit_mode, UnitMode) else UnitMode.parse(str(unit_mode))
    if mode == UnitMode.REQUIRE:
        return True
    if mode == UnitMode.IGNORE:
        return False
    return any(str(row.unit).strip() for row in reference_rows)


def _filter_predictions_by_reference_scope(
    prediction_rows: list[SegmentQualityRow],
    reference_rows: list[SegmentQualityRow],
    scope: PredictionScope | str,
) -> tuple[list[SegmentQualityRow], list[SegmentQualityRow]]:
    mode = scope if isinstance(scope, PredictionScope) else PredictionScope.parse(str(scope))
    if mode == PredictionScope.ALL:
        return prediction_rows, []
    strict_main = mode == PredictionScope.MAIN_STRICT
    allowed = {
        (row.dataset, _canon_pdf(row.document), _canon_enum(row.enum))
        for row in reference_rows
    }
    kept: list[SegmentQualityRow] = []
    ignored: list[SegmentQualityRow] = []
    for row in prediction_rows:
        if strict_main and not _canon_segment(row.segment):
            kept.append(row)
            continue
        key = (row.dataset, _canon_pdf(row.document), _canon_enum(row.enum))
        if key in allowed:
            kept.append(row)
        else:
            ignored.append(row)
    return kept, ignored


def _dataset_uses_indian_fiscal_calendar(dataset: str) -> bool:
    value = str(dataset or "").strip().lower().replace("-", "_").replace(" ", "_")
    return bool(value in {"ind", "india"} or value.startswith(("ind_", "india_")) or "ind_as" in value or "ind_gaap" in value)


def _looks_like_normalized_period_label(period: object) -> bool:
    value = str(period or "").replace("\u00a0", " ").strip()
    return bool(
        re.fullmatch(r"(?:FY|Q[1-4]|[369]M|12M) 20\d{2}", value)
        or re.fullmatch(r"\d{1,2}[.]\d{2}[.]20\d{2}", value)
        or re.fullmatch(r"\d{1,2}M \d{1,2}[.]\d{2}[.]20\d{2}", value)
    )


def _event_period_for_scoring(pv: dict[str, Any], enum: str, dataset: str = "") -> str:
    raw_period = pv.get("raw_period") or ""
    normalized_period = str(pv.get("period") or "").strip()
    if (
        raw_period
        and _dataset_uses_indian_fiscal_calendar(dataset)
        and not _looks_like_normalized_period_label(raw_period)
    ):
        period = normalize_indian_fiscal_period_label(
            raw_period,
            value_kind=period_kind_for_enum(enum),
        )
        if period and period != str(raw_period).replace("\u00a0", " ").strip():
            return normalize_metric_period(period, enum).period
    if raw_period:
        return normalize_metric_period(
            raw_period,
            enum,
            normalized_period=normalized_period or None,
        ).period
    if normalized_period:
        return normalize_metric_period(normalized_period, enum).period
    return ""


def metric_events_to_rows(
    metrics: list[dict[str, Any]],
    *,
    dataset: str,
    default_document: str = "",
    include_main_rows: bool = False,
    include_segment_rows: bool = True,
    legacy_replay: bool = False,
) -> list[SegmentQualityRow]:
    rows: list[SegmentQualityRow] = []
    row_id = 0
    direct_enums: set[str] = set()
    for metric in metrics:
        if metric.get("type") not in (None, "metric"):
            continue
        segment = str(metric.get("segment") or "").strip()
        if segment and not include_segment_rows:
            continue
        if not segment and not include_main_rows:
            continue
        if not metric.get("is_calculated"):
            direct_enums.add(_canon_enum(str(metric.get("enum") or "")))
    for metric in metrics:
        if metric.get("type") not in (None, "metric"):
            continue
        segment = str(metric.get("segment") or "").strip()
        if segment and not include_segment_rows:
            continue
        if not segment and not include_main_rows:
            continue
        document = str(
            metric.get("pdf_name")
            or metric.get("document")
            or metric.get("filename")
            or default_document
        )
        metric_dataset = str(metric.get("dataset") or dataset)
        enum = _canon_enum(str(metric.get("enum") or ""))
        if metric.get("is_calculated") and enum in direct_enums:
            continue
        unit = _display_unit_for_enum(enum, str(metric.get("unit") or ""))
        for pv in metric.get("period_values") or []:
            if not isinstance(pv, dict):
                continue
            row_id += 1
            value = (
                _value_with_source_sign(pv.get("value"), pv.get("source_text"))
                if legacy_replay
                else pv.get("value")
            )
            if value is None and pv.get("source_text"):
                value = _num(pv.get("source_text"))
            if value is None:
                continue
            rows.append(
                SegmentQualityRow(
                    dataset=metric_dataset,
                    document=document,
                    enum=enum,
                    period=_event_period_for_scoring(pv, enum, metric_dataset),
                    segment=segment,
                    value=_canonical_value(value),
                    unit=_display_unit_for_enum(enum, str(pv.get("unit") or unit)),
                    segment_type=str(metric.get("segment_type") or ""),
                    source="pred",
                    row_id=row_id,
                )
            )
    return rows


def metric_events_to_table_rows(
    metrics: list[dict[str, Any]],
    *,
    dataset: str,
    default_document: str = "",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric in metrics:
        if metric.get("type") not in (None, "metric"):
            continue
        document = str(metric.get("pdf_name") or default_document)
        enum = _canon_enum(str(metric.get("enum") or ""))
        segment = str(metric.get("segment") or "")
        dimension = str(metric.get("dimension") or "")
        unit = _display_unit_for_enum(enum, str(metric.get("unit") or ""))
        page = metric.get("page")
        period_values = metric.get("period_values") or []
        if isinstance(period_values, list) and period_values:
            for pv in period_values:
                if not isinstance(pv, dict):
                    continue
                rows.append(
                    {
                        "dataset": dataset,
                        "document": document,
                        "enum": enum,
                        "period": _event_period_for_scoring(pv, enum, dataset),
                        "segment": segment,
                        "dimension": dimension,
                        "value": _canonical_value(pv.get("value")),
                        "unit": _display_unit_for_enum(enum, str(pv.get("unit") or unit)),
                        "page": pv.get("page") or page or "",
                        "source_text": str(pv.get("source_text") or ""),
                        "found": metric.get("found", ""),
                        "is_calculated": metric.get("is_calculated", False),
                    }
                )
            continue
        rows.append(
            {
                "dataset": dataset,
                "document": document,
                "enum": enum,
                "period": "",
                "segment": segment,
                "dimension": dimension,
                "value": _canonical_value(metric.get("value_current")),
                "unit": unit,
                "page": page or "",
                "source_text": "",
                "found": metric.get("found", ""),
                "is_calculated": metric.get("is_calculated", False),
            }
        )
    return rows


def load_mock_prediction_rows(
    path: Path,
    *,
    default_dataset: str,
    pdf_filter: set[str] | None = None,
    include_segments: bool = True,
    include_main_rows: bool | None = None,
    legacy_replay: bool = False,
) -> list[SegmentQualityRow]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        metrics = payload.get("metrics") or payload.get("events") or []
        default_document = str(payload.get("pdf_name") or payload.get("document") or "")
        default_dataset = str(payload.get("dataset") or default_dataset)
    elif isinstance(payload, list):
        metrics = payload
        default_document = ""
    else:
        raise ValueError(f"Unsupported mock predictions shape in {path}")
    rows = metric_events_to_rows(
        list(metrics),
        dataset=default_dataset,
        default_document=default_document,
        include_main_rows=(not include_segments if include_main_rows is None else include_main_rows),
        include_segment_rows=include_segments,
        legacy_replay=legacy_replay,
    )
    if pdf_filter:
        allowed = {_canon_pdf(item) for item in pdf_filter}
        rows = [row for row in rows if _canon_pdf(row.document) in allowed]
    return rows


PROMPT_TEXT_MODES = {"default", "duplicate_tables", "no_tables", "tables_context", "text_only"}
_PROMPT_TEXT_MODE_ALIASES = {
    "text_only": "no_tables",
}


def _normalize_prompt_text_mode(mode: str | None) -> str:
    normalized = (mode or "default").strip().lower().replace("-", "_")
    normalized = _PROMPT_TEXT_MODE_ALIASES.get(normalized, normalized)
    if normalized not in PROMPT_TEXT_MODES:
        allowed = ", ".join(sorted(PROMPT_TEXT_MODES))
        raise ValueError(f"Unsupported prompt text mode {mode!r}; expected one of: {allowed}")
    return normalized


def _is_markdown_table_separator(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped:
        return False
    body = stripped.strip("|").strip()
    return bool(body) and all(ch in "-:| " for ch in body)


def _is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _strip_markdown_tables(text: str) -> str:
    """Remove pipe-table blocks from OCR markdown, preserving surrounding text."""
    lines = text.splitlines()
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
        if _is_markdown_table_line(line) and _is_markdown_table_separator(next_line):
            idx += 2
            while idx < len(lines) and _is_markdown_table_line(lines[idx]):
                idx += 1
            if out and out[-1].strip():
                out.append("")
            continue
        out.append(line)
        idx += 1
    return "\n".join(out)


def _table_prompt_block(table: dict[str, Any], idx: int, *, include_context: bool) -> str:
    table_text = (
        table.get("text")
        or table.get("formatted_str")
        or table.get("original_text")
        or ""
    )
    if not table_text:
        return ""
    page = table.get("page") or ""
    header = f"===PAGE {page}===\n# OCR table {idx}"
    if not include_context:
        return f"{header}\n{table_text}"
    before = str(table.get("before_context") or "").strip()
    after = str(table.get("after_context") or "").strip()
    parts = [header]
    if before:
        parts.append("# Previous context")
        parts.append(before)
    parts.append(table_text)
    if after:
        parts.append("# After context")
        parts.append(after)
    return "\n".join(parts)


def _clamp_table_context_lines(value: int | None) -> int:
    try:
        lines = int(value if value is not None else 2)
    except (TypeError, ValueError):
        lines = 2
    return max(0, min(lines, 20))


def _tables_with_neighbor_context(
    text: str,
    tables: list[dict[str, Any]],
    *,
    table_context_lines: int,
) -> list[dict[str, Any]]:
    """Return table copies with before/after context recomputed for experiments."""
    context_lines = _clamp_table_context_lines(table_context_lines)
    table_copies = [dict(table) for table in tables]
    if not table_copies:
        return table_copies
    from finengine.skills.aifa_baseline.idp_ocr_client import _attach_table_positions_and_context

    _attach_table_positions_and_context(
        text,
        table_copies,
        pre_lines=context_lines,
        post_lines=context_lines,
    )
    return table_copies


def _format_ocr_text_for_prompt(
    text: str,
    tables: list[dict[str, Any]],
    *,
    prompt_text_mode: str,
    table_context_lines: int = 2,
) -> str:
    mode = _normalize_prompt_text_mode(prompt_text_mode)
    if mode == "no_tables":
        return _strip_markdown_tables(text)
    if mode == "duplicate_tables":
        table_blocks = [
            block
            for i, table in enumerate(tables, start=1)
            if (block := _table_prompt_block(table, i, include_context=False))
        ]
        return "\n\n".join([part for part in [text, *table_blocks] if part.strip()])
    if mode == "tables_context":
        tables = _tables_with_neighbor_context(
            text,
            tables,
            table_context_lines=table_context_lines,
        )
        table_blocks = [
            block
            for i, table in enumerate(tables, start=1)
            if (block := _table_prompt_block(table, i, include_context=True))
        ]
        return "\n\n".join([part for part in [text, *table_blocks] if part.strip()])
    return text


def _ocr_result_from_saved_idp_response(raw: dict[str, Any], *, table_context_lines: int = 2):
    """Rebuild the same OcrResult shape that call_idp_ocr returns from cache."""
    from finengine.skills.aifa_baseline.idp_ocr_client import (
        OcrPage,
        OcrResult,
        _attach_table_positions_and_context,
        _normalize_tables_in_text,
        _page_content_from_response,
        _page_markdown_block,
    )

    pages: list[OcrPage] = []
    flat_tables: list[dict[str, Any]] = []
    full_md_parts: list[str] = []
    failed_pages: list[dict[str, Any]] = []
    results = raw.get("results", []) if isinstance(raw, dict) else []
    for result in sorted(results, key=lambda item: item.get("page", 0)):
        if not isinstance(result, dict) or not result.get("success"):
            failed_pages.append({"page": result.get("page", ""), "error": str(result.get("error", ""))[:200]})
            continue
        page_num = int(result.get("page") or 0)
        content = _page_content_from_response(result.get("response", {}))
        if not content:
            failed_pages.append({"page": page_num, "error": "empty markdown"})
            continue
        content_md, all_tables = _normalize_tables_in_text(content)
        pages.append(OcrPage(page=page_num, markdown=content_md, tables=[t["text"] for t in all_tables]))
        full_md_parts.append(_page_markdown_block(page_num, content_md))
        for table in all_tables:
            flat_tables.append(
                {
                    "idx": len(flat_tables),
                    "page": page_num,
                    "text": table["text"],
                    "original_text": table.get("original_text") or table["text"],
                }
            )
    full_text = "\n\n".join(full_md_parts)
    _attach_table_positions_and_context(
        full_text,
        flat_tables,
        pre_lines=_clamp_table_context_lines(table_context_lines),
        post_lines=_clamp_table_context_lines(table_context_lines),
    )
    return OcrResult(
        pages=pages,
        full_text=full_text,
        full_tables=flat_tables,
        diag={
            "pages_total": raw.get("pages_count", len(results)) if isinstance(raw, dict) else len(results),
            "pages_success": len(pages),
            "pages_failed": len(failed_pages),
            "failed_samples": failed_pages[:5],
            "tables_total": len(flat_tables),
            "from_saved_raw": True,
        },
    )


def _ocr_result_to_prompt_text(
    ocr_result: Any,
    *,
    prompt_text_mode: str,
    table_context_lines: int = 2,
) -> str:
    return _format_ocr_text_for_prompt(
        str(getattr(ocr_result, "full_text", "") or ""),
        list(getattr(ocr_result, "full_tables", []) or []),
        prompt_text_mode=prompt_text_mode,
        table_context_lines=table_context_lines,
    )


def _ocr_json_to_text(
    path: Path,
    *,
    prompt_text_mode: str = "default",
    table_context_lines: int = 2,
) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    ocr = data.get("ocr_result") or data
    mode = _normalize_prompt_text_mode(prompt_text_mode)
    full_text = str(ocr.get("full_text") or "")
    full_tables = list(ocr.get("full_tables") or [])
    if full_text or full_tables:
        from types import SimpleNamespace

        return _ocr_result_to_prompt_text(
            SimpleNamespace(
                full_text=full_text or str(ocr.get("text") or ""),
                full_tables=full_tables or list(ocr.get("tables") or []),
            ),
            prompt_text_mode=mode,
            table_context_lines=table_context_lines,
        )
    text = str(ocr.get("text") or "")
    tables = list(ocr.get("tables") or [])
    if mode == "no_tables":
        return text
    parts: list[str] = []
    if text:
        parts.append(text)
    include_context = mode == "tables_context"
    if include_context:
        tables = _tables_with_neighbor_context(
            text,
            tables,
            table_context_lines=table_context_lines,
        )
    for i, table in enumerate(tables, start=1):
        block = _table_prompt_block(table, i, include_context=include_context)
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def _ocr_json_pdf_name(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        document = str(data.get("document") or "")
        if document.lower().endswith(".pdf"):
            return Path(document).name
    except Exception:
        pass
    if path.name.lower().endswith(".pdf.json"):
        return path.name[:-5]
    return path.with_suffix(".pdf").name


def _idp_cache_path_for_pdf(path: Path) -> Path:
    return path.parent.parent / "idpocr_cache" / f"{path.stem}.idpocr.v2.json"


async def _pdf_to_text(
    path: Path,
    *,
    dataset: str,
    company_type: str,
    prompt_text_mode: str = "default",
    force_idp_ocr: bool = False,
    table_context_lines: int = 2,
) -> str:
    """Build the production-like full-text sidecar from a PDF fixture.

    Reuse the raw IDP response when available. Otherwise call the same IDP-OCR
    client used by the dev upload path and persist the response for later runs.
    Legacy flat OCR fixtures are intentionally not used in PDF mode because
    they do not reproduce the frontend's paginated Markdown input.
    """
    from finengine.skills.aifa_baseline.idp_ocr_client import (
        call_idp_ocr,
        markdown_from_saved_idp_response,
    )

    cache_path = _idp_cache_path_for_pdf(path)
    if cache_path.exists():
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        if _normalize_prompt_text_mode(prompt_text_mode) == "default":
            text = markdown_from_saved_idp_response(raw)
        else:
            text = _ocr_result_to_prompt_text(
                _ocr_result_from_saved_idp_response(raw, table_context_lines=table_context_lines),
                prompt_text_mode=prompt_text_mode,
                table_context_lines=table_context_lines,
            )
        if text.strip():
            return text

    sibling_ocr = path.with_suffix(".json")
    if sibling_ocr.exists() and not force_idp_ocr:
        text = _ocr_json_to_text(
            sibling_ocr,
            prompt_text_mode=prompt_text_mode,
            table_context_lines=table_context_lines,
        )
        if text.strip():
            return text

    ocr = await call_idp_ocr(path, save_raw_to=cache_path)
    if not ocr.full_text.strip():
        raise RuntimeError(f"IDP-OCR returned empty text for {path}")
    return _ocr_result_to_prompt_text(
        ocr,
        prompt_text_mode=prompt_text_mode,
        table_context_lines=table_context_lines,
    )


async def _input_to_text(
    path: Path,
    *,
    dataset: str,
    company_type: str,
    prompt_text_mode: str = "default",
    force_idp_ocr: bool = False,
    table_context_lines: int = 2,
) -> str:
    if path.suffix.lower() == ".pdf":
        return await _pdf_to_text(
            path,
            dataset=dataset,
            company_type=company_type,
            prompt_text_mode=prompt_text_mode,
            force_idp_ocr=force_idp_ocr,
            table_context_lines=table_context_lines,
        )
    return _ocr_json_to_text(
        path,
        prompt_text_mode=prompt_text_mode,
        table_context_lines=table_context_lines,
    )


def _input_pdf_name(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return path.name
    return _ocr_json_pdf_name(path)


def _input_path_pdf_name(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return path.name
    if path.name.lower().endswith(".pdf.json"):
        return path.name[:-5]
    return path.with_suffix(".pdf").name


class _IndexStub:
    def __init__(self, meta: DocumentMeta):
        self.meta = meta

    def get_document(self, doc_id: str):
        return self.meta if doc_id == self.meta.doc_id else None

    def get_chunks_by_type(self, *args, **kwargs):
        return []


async def _run_real_one(
    *,
    dataset: str,
    json_path: Path,
    run_dir: Path,
    model_id: str | None,
    limit: int | None,
    include_segments: bool,
    include_main_rows: bool | None = None,
    include_consolidated_group: bool = False,
    metric_enums: set[str] | None = None,
    company_type_override: str = "non_financial",
    document_type_override: str = "financial_report",
    role: str | None = None,
    extractor_matrix: bool = False,
    legacy_replay: bool = False,
    segment_mode: SegmentMode | str = SegmentMode.EXTENDED,
    segment_context_mode: str = "full",
    prompt_text_mode: str = "default",
    force_idp_ocr: bool = False,
    table_context_lines: int = 2,
    document_name_override: str | None = None,
    docs_dir_override: Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[list[SegmentQualityRow], list[dict[str, Any]], dict[str, Any]]:
    """Run the same Finengine DEV registry extraction used by the frontend."""
    def _progress(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    input_kind = "PDF/OCR" if json_path.suffix.lower() == ".pdf" else "OCR JSON"
    _progress(f"loading {input_kind} and assembling prompt text")
    text = await _input_to_text(
        json_path,
        dataset=dataset,
        company_type=company_type_override,
        prompt_text_mode=prompt_text_mode,
        force_idp_ocr=force_idp_ocr,
        table_context_lines=table_context_lines,
    )
    filename = document_name_override or _input_pdf_name(json_path)
    _progress(f"prompt text ready ({len(text):,} chars); preparing extraction")
    doc_id = f"finengine-e2e-{dataset}-{json_path.stem}".replace("/", "_")
    parsed_company_type = CompanyType(company_type_override)
    parsed_document_type = DocType(document_type_override)
    meta = DocumentMeta(
        doc_id=doc_id,
        filename=filename,
        doc_type=parsed_document_type,
        company_type=parsed_company_type,
        company_name=json_path.stem,
        period="",
        owner_id=0,
    )

    event_path = run_dir / "events" / f"{dataset}_{Path(filename).stem}.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir = run_dir / "llm_io" / dataset / Path(filename).stem
    metrics: list[dict[str, Any]] = []
    all_metric_events: list[dict[str, Any]] = []
    meta_event: dict[str, Any] = {}
    score_main_rows = not include_segments if include_main_rows is None else include_main_rows
    started = time.time()

    async def _run_with_docs_dir(docs_dir: Path) -> None:
        nonlocal meta_event
        _progress("writing temporary document text for extractor")
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / f"{doc_id}.txt").write_text(text, encoding="utf-8")
        idx = _IndexStub(meta)
        try:
            with event_path.open("w", encoding="utf-8") as f:
                _progress("starting Finengine DEV extractor")
                effective_role = role if role is not None else ("ДИЭ" if include_segments else "")
                event_iter = None
                routed_to = "legacy:finengine_dev"
                profile_event: dict[str, Any] | None = None
                if extractor_matrix:
                    try:
                        from finengine.skills.extractors import ExtractContext, detect_profile, get_extractor

                        profile = detect_profile(text[:200_000], standard_text=text)
                        profile_event = {
                            "type": "profile_detected",
                            "profile_id": profile.profile_id,
                            "standard": profile.standard,
                            "language": profile.language,
                            "company_type": profile.company_type,
                            "confidence_low": profile.confidence_low,
                            "reason": profile.reason,
                        }
                        f.write(json.dumps(profile_event, ensure_ascii=False) + "\n")
                        _progress(f"profile detected: {profile.profile_id}; preparing matrix extractor")
                        cell_stream = get_extractor(profile.profile_id)
                        if cell_stream is not None:
                            routed_to = f"matrix:{profile.profile_id}"
                            event_iter = cell_stream(
                                ExtractContext(
                                    doc_id=doc_id,
                                    pdf_text=text,
                                    company_type=company_type_override,
                                    model_id=model_id,
                                    metric_enums=sorted(metric_enums) if metric_enums else None,
                                    role=effective_role,
                                    include_segments=include_segments,
                                    include_consolidated_group=include_consolidated_group,
                                    segment_mode=SegmentMode.parse(str(segment_mode)).value,
                                    segment_context_mode=segment_context_mode,
                                    limit=limit,
                                    index=idx,
                                    debug_dir=str(debug_dir),
                                    profile_reason=profile.reason,
                                )
                            )
                        else:
                            routed_to = f"matrix_fallback:{profile.profile_id}"
                    except Exception as exc:
                        routed_to = f"matrix_classifier_error:{type(exc).__name__}"
                if event_iter is None:
                    _progress("using direct finengine_dev extractor")
                    event_iter = extract_stream_finengine_dev(
                        doc_id,
                        company_type_override,
                        index=idx,
                        limit=limit,
                        model_id=model_id,
                        include_segments=include_segments,
                        include_consolidated_group=include_consolidated_group,
                        metric_enums=metric_enums,
                        role=effective_role,
                        segment_mode=SegmentMode.parse(str(segment_mode)).value,
                        segment_context_mode=segment_context_mode,
                        debug_dir=debug_dir,
                    )
                _progress(f"waiting for extractor stream / LLM response ({routed_to})")
                async for event in event_iter:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    if event.get("type") == "metric":
                        event = dict(event)
                        event["dataset"] = dataset
                        event["pdf_name"] = filename
                        all_metric_events.append(event)
                        if event.get("segment") or score_main_rows:
                            metrics.append(event)
                        if len(all_metric_events) % 25 == 0:
                            _progress(f"receiving metric events ({len(all_metric_events)} so far)")
                    elif event.get("type") == "done":
                        meta_event = event
                        _progress("extractor completed; converting events")
                    elif event.get("type") == "error":
                        meta_event = {"error": event.get("message")}
                        _progress(f"extractor returned error: {event.get('message')}")
                        break
                if profile_event:
                    meta_event.setdefault("_diag", {})
                    if isinstance(meta_event["_diag"], dict):
                        meta_event["_diag"]["profile_detected"] = profile_event
                meta_event.setdefault("_diag", {})
                if isinstance(meta_event["_diag"], dict):
                    meta_event["_diag"]["routed_to"] = routed_to
        finally:
            pass

    if docs_dir_override is not None:
        await _run_with_docs_dir(docs_dir_override)
    else:
        old_docs_dir = settings.docs_dir
        with tempfile.TemporaryDirectory(prefix="finengine-e2e-") as tmp:
            settings.docs_dir = Path(tmp)
            try:
                await _run_with_docs_dir(settings.docs_dir)
            finally:
                settings.docs_dir = old_docs_dir
    _progress(f"converting {len(metrics)} scored metric event(s) to rows")
    meta_event["elapsed_s"] = round(time.time() - started, 1)
    meta_event["events_path"] = str(event_path)
    meta_event["extractor_matrix"] = extractor_matrix
    meta_event["llm_io_dir"] = str(debug_dir)
    meta_event["prompt_text_mode"] = _normalize_prompt_text_mode(prompt_text_mode)
    meta_event["force_idp_ocr"] = force_idp_ocr
    meta_event["table_context_lines"] = _clamp_table_context_lines(table_context_lines)
    return (
        metric_events_to_rows(
            metrics,
            dataset=dataset,
            default_document=filename,
            include_main_rows=score_main_rows,
            include_segment_rows=include_segments,
            legacy_replay=legacy_replay,
        ),
        metric_events_to_table_rows(all_metric_events, dataset=dataset, default_document=filename),
        meta_event,
    )


def _event_cache_file(cache_path: Path, dataset: str, filename: str) -> Path:
    if cache_path.is_file():
        return cache_path
    base = cache_path / "events" if (cache_path / "events").is_dir() else cache_path
    return base / f"{dataset}_{Path(filename).stem}.jsonl"


def _read_event_cache(
    *,
    dataset: str,
    event_path: Path,
    default_document: str,
    include_segments: bool = True,
    include_main_rows: bool | None = None,
    legacy_replay: bool = False,
) -> tuple[list[SegmentQualityRow], list[dict[str, Any]], dict[str, Any]]:
    if not event_path.exists():
        raise FileNotFoundError(f"Replay event file not found: {event_path}")
    metrics: list[dict[str, Any]] = []
    all_metric_events: list[dict[str, Any]] = []
    meta_event: dict[str, Any] = {"events_path": str(event_path)}
    score_main_rows = not include_segments if include_main_rows is None else include_main_rows
    for line_no, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL event at {event_path}:{line_no}") from exc
        if event.get("type") == "metric" and (event.get("segment") or score_main_rows):
            event = dict(event)
            event.setdefault("dataset", dataset)
            event.setdefault("pdf_name", default_document)
            all_metric_events.append(event)
            metrics.append(event)
        elif event.get("type") == "metric":
            event = dict(event)
            event.setdefault("dataset", dataset)
            event.setdefault("pdf_name", default_document)
            all_metric_events.append(event)
        elif event.get("type") == "done":
            meta_event.update(event)
        elif event.get("type") == "profile_detected":
            # Keep the classifier verdict (standard/language/company_type/
            # confidence) so a replayed run isn't logged as unknown language /
            # classifier status downstream.
            meta_event["profile_detected"] = event
        elif event.get("type") == "error":
            meta_event["error"] = event.get("message", "unknown_error")
    meta_event["replayed"] = True
    return (
        metric_events_to_rows(
            metrics,
            dataset=dataset,
            default_document=default_document,
            include_main_rows=score_main_rows,
            include_segment_rows=include_segments,
            legacy_replay=legacy_replay,
        ),
        metric_events_to_table_rows(all_metric_events, dataset=dataset, default_document=default_document),
        meta_event,
    )


def _preset_dataset_inputs(
    dataset: str,
    gt_kind: GroundTruthKind | str = GroundTruthKind.SEGMENT_ONLY,
    *,
    input_mode: str = "pdf",
    ocr_json_variant: str = "old",
) -> list[DatasetInput]:
    normalized = dataset.lower().replace("-", "").replace("_", "")
    # Kept for CLI compatibility; golden CSVs already contain the complete GT.
    GroundTruthKind.parse(str(gt_kind))
    mode = (input_mode or "pdf").strip().lower()
    if mode not in {"json", "pdf"}:
        raise ValueError("--input-mode must be json or pdf")
    variant = _normalize_ocr_json_variant(ocr_json_variant)

    preset_keys: list[str] = []
    if normalized in {"ifrs", "both", "all"}:
        preset_keys.append("ifrs_nf_ru")
    if normalized in {"usgaap", "usgaapnfeng", "both", "all"}:
        preset_keys.append("us_gaap_nf_eng")
    if normalized in {"ifrsbank", "ifrsbankru", "bank", "all"}:
        preset_keys.append("ifrs_bank_ru")
    if not preset_keys:
        raise ValueError("dataset must be one of: ifrs, usgaap, ifrsbank, both, all, custom")

    out: list[DatasetInput] = []
    suffix = ".pdf" if mode == "pdf" else ".json"
    input_dir_name = "pdf" if mode == "pdf" else "json"
    for preset_key in preset_keys:
        dataset_name, golden_dir_name = DATASET_PRESETS[preset_key]
        root = GOLDEN_ROOT / golden_dir_name
        for gt_path in sorted((root / "gt").glob("*.csv")):
            if gt_path.name == "gt.csv":
                continue
            input_paths = tuple((root / input_dir_name).glob(f"{gt_path.stem}{suffix}"))
            if mode == "json":
                input_paths = _filter_ocr_json_variant(input_paths, variant)
            if not input_paths:
                continue
            out.append(
                DatasetInput(
                    name=dataset_name,
                    gt_xlsx=gt_path,
                    json_paths=input_paths,
                    root_dir=root,
                    input_mode=mode,
                )
            )
    if not out:
        raise ValueError(f"No complete golden GT/{mode} pairs found for dataset {dataset!r}")
    return out


def _expand_json_inputs(paths: Iterable[Path]) -> list[Path]:
    json_paths: list[Path] = []
    for path in paths:
        if path.is_dir():
            json_paths.extend(sorted(p for p in path.glob("*.json") if p.is_file()))
        elif path.is_file():
            json_paths.append(path)
        else:
            raise FileNotFoundError(f"OCR JSON path does not exist: {path}")
    seen: set[Path] = set()
    out: list[Path] = []
    for path in json_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def _expand_pdf_inputs(paths: Iterable[Path]) -> list[Path]:
    pdf_paths: list[Path] = []
    for path in paths:
        if path.is_dir():
            pdf_paths.extend(sorted(p for p in path.glob("*.pdf") if p.is_file()))
        elif path.is_file() and path.suffix.lower() == ".pdf":
            pdf_paths.append(path)
        else:
            raise FileNotFoundError(f"PDF input path does not exist or is not a PDF: {path}")
    seen: set[Path] = set()
    out: list[Path] = []
    for path in pdf_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def dataset_inputs(
    dataset: str,
    *,
    gt_kind: GroundTruthKind | str = GroundTruthKind.SEGMENT_ONLY,
    gt_xlsx: Path | None = None,
    json_dirs: list[Path] | None = None,
    json_paths: list[Path] | None = None,
    pdf_dirs: list[Path] | None = None,
    pdf_paths: list[Path] | None = None,
    input_mode: str = "pdf",
    ocr_json_variant: str = "old",
    dataset_name: str | None = None,
    gt_sheet: str | None = None,
) -> list[DatasetInput]:
    normalized = dataset.lower().replace("-", "").replace("_", "")
    mode = (input_mode or "pdf").strip().lower()
    if mode not in {"json", "pdf"}:
        raise ValueError("--input-mode must be json or pdf")
    variant = _normalize_ocr_json_variant(ocr_json_variant)
    custom_json_inputs = [*(json_dirs or []), *(json_paths or [])]
    custom_pdf_inputs = [*(pdf_dirs or []), *(pdf_paths or [])]
    custom_inputs = custom_pdf_inputs if mode == "pdf" else custom_json_inputs
    has_custom = gt_xlsx is not None or bool(custom_inputs) or bool(dataset_name) or bool(gt_sheet)
    if normalized == "custom":
        if gt_xlsx is None:
            raise ValueError("--dataset custom requires --gt-xlsx")
        expanded = _expand_pdf_inputs(custom_pdf_inputs) if mode == "pdf" else list(
            _filter_ocr_json_variant(_expand_json_inputs(custom_json_inputs), variant)
        )
        if not expanded:
            raise ValueError("--dataset custom requires --pdf-dir/--pdf-path when --input-mode pdf, or --json-dir/--json-path")
        return [
            DatasetInput(
                name=dataset_name or "CUSTOM",
                gt_xlsx=gt_xlsx,
                json_paths=tuple(expanded),
                gt_sheet=gt_sheet,
                input_mode=mode,
            )
        ]

    presets = _preset_dataset_inputs(dataset, gt_kind=gt_kind, input_mode=mode, ocr_json_variant=variant)
    if not has_custom:
        return presets
    if len(presets) != 1:
        raise ValueError(
            "Custom --gt-xlsx/--json-dir/--json-path overrides require --dataset ifrs/usgaap/ifrsbank, "
            "or use --dataset custom with all paths."
        )

    preset = presets[0]
    expanded_json = (
        (
            _expand_pdf_inputs(custom_pdf_inputs)
            if mode == "pdf"
            else list(_filter_ocr_json_variant(_expand_json_inputs(custom_json_inputs), variant))
        )
        if custom_inputs
        else list(preset.json_paths)
    )
    return [
        DatasetInput(
            name=dataset_name or preset.name,
            gt_xlsx=gt_xlsx or preset.gt_xlsx,
            json_paths=tuple(expanded_json),
            root_dir=preset.root_dir,
            gt_sheet=gt_sheet,
            input_mode=mode,
        )
    ]


def resolve_pdf_filter(dataset_items: list[DatasetInput], raw_filters: list[str], include_segments: bool) -> set[str] | None:
    if not raw_filters:
        return None
    all_docs: dict[str, str] = {}
    for item in dataset_items:
        for row in load_reference_rows(
            item.gt_xlsx,
            item.name,
            include_segments=include_segments,
            sheet_name=item.gt_sheet,
        ):
            all_docs[_canon_pdf(row.document)] = row.document
    resolved: set[str] = set()
    for raw in raw_filters:
        needle = _canon_pdf(raw)
        matches = [doc for key, doc in all_docs.items() if key == needle or needle in key]
        if not matches:
            options = ", ".join(sorted(all_docs.values())[:30])
            raise ValueError(f"PDF filter {raw!r} did not match any GT document. Options: {options}")
        resolved.update(matches)
    return resolved


def _json_paths_for_docs(dataset_item: DatasetInput, pdf_filter: set[str] | None) -> list[Path]:
    return [path for path, _ in _json_inputs_for_docs(dataset_item, pdf_filter)]


def _json_inputs_for_docs(dataset_item: DatasetInput, pdf_filter: set[str] | None) -> list[tuple[Path, str | None]]:
    json_paths = list(dataset_item.json_paths)
    if not pdf_filter:
        return [(path, None) for path in json_paths]
    allowed_exact = {_canon_pdf(item): item for item in pdf_filter}
    allowed_fuzzy: dict[str, str] = {}
    for item in pdf_filter:
        allowed_fuzzy.setdefault(_doc_match_key(item), item)
    selected: list[tuple[Path, str | None]] = []
    for path in json_paths:
        input_name = _input_path_pdf_name(path)
        exact = _canon_pdf(input_name)
        if exact in allowed_exact:
            selected.append((path, allowed_exact[exact]))
            continue
        fuzzy = _doc_match_key(input_name)
        if fuzzy in allowed_fuzzy:
            selected.append((path, allowed_fuzzy[fuzzy]))
    if selected:
        return selected
    return []


def _slug(value: str) -> str:
    text = nfc(value).strip()
    text = re.sub(r"\.[Pp][Dd][Ff]$", "", text)
    text = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", text)
    return text.strip("._-") or "all"


def _model_slug(model_id: str | None) -> str:
    if not model_id:
        return "default_model"
    return _slug(model_id.split("/")[-1].replace(".", "_"))


def make_run_dir(
    base_dir: Path,
    prefix: str,
    *,
    pdf_filters: list[str] | None = None,
    model_id: str | None = None,
) -> Path:
    stamp = datetime.now().strftime("%d_%m_%y_%H_%M")
    doc_part = _slug("_".join(pdf_filters or []) or "all")
    run_name = f"{_slug(prefix)}_{stamp}_{doc_part}_{_model_slug(model_id)}"
    run_dir = base_dir / run_name
    counter = 2
    while run_dir.exists():
        run_dir = base_dir / f"{run_name}_{counter}"
        counter += 1
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _metric_rows(metrics: Iterable[EvalMetrics]) -> list[dict[str, Any]]:
    return [metric.to_flat_dict() for metric in metrics]


def _format_metrics_line(metric: EvalMetrics, *, include_business: bool = True) -> str:
    business = (
        f"bP={metric.values.business_precision:.1%} "
        f"bR={metric.values.business_recall:.1%} "
        f"bF1={metric.values.business_f1:.1%} "
        f"bF0.5={metric.values.business_f05:.1%} "
        if include_business
        else ""
    )
    return (
        f"{metric.scope}: P={metric.values.precision:.1%} "
        f"R={metric.values.recall:.1%} F1={metric.values.f1:.1%} "
        f"F0.5={metric.values.f05:.1%} "
        f"{business}"
        f"TP={metric.counts.TP} "
        f"EFP={metric.counts.EFP} EFN={metric.counts.EFN} EV={metric.counts.EV}"
    )


def _format_cli_metric(result: EvaluationResult | None, *, no_rows: str = "no GT rows") -> str:
    if result is None:
        return no_rows
    if len(result.by_document) == 1:
        metric = next(iter(result.by_document.values()))
    else:
        metric = result.micro
    return _format_metrics_line(metric, include_business=False)


def _format_cli_summary(run: SegmentQualityRun) -> list[str]:
    optional_ref_rows = sum(
        1
        for row in run.reference_rows
        if _canon_segment(row.segment) and _canon_segment_type(row.segment_type) == "optional"
    )
    lines = [
        "OVERALL CORE:",
        f"  {_format_cli_metric(run.overall_core_result)}",
        "OVERALL ALL:",
        f"  {_format_cli_metric(run.overall_all_result)}",
        "MAIN:",
        f"  {_format_cli_metric(run.main_result)}",
    ]
    lines.append("SEGMENTS CORE:")
    lines.append(f"  {_format_cli_metric(run.segment_core_result)}")
    lines.append("SEGMENTS ALL:")
    lines.append(f"  {_format_cli_metric(run.segment_all_result)}")
    lines.append(
        f"OPTIONAL SEGMENTS: {optional_ref_rows} GT rows included in SEGMENTS ALL"
        if optional_ref_rows
        else "OPTIONAL SEGMENTS: no GT rows"
    )
    return lines


def _reference_profile(rows: list[SegmentQualityRow]) -> dict[str, Any]:
    main_rows = sum(1 for row in rows if not _canon_segment(row.segment))
    segment_rows = len(rows) - main_rows
    total_rows = sum(1 for row in rows if _canon_segment(row.segment) == "total")
    required_segment_rows = sum(
        1 for row in rows
        if _canon_segment(row.segment) and _canon_segment_type(row.segment_type) == "required"
    )
    optional_segment_rows = sum(
        1 for row in rows
        if _canon_segment(row.segment) and _canon_segment_type(row.segment_type) == "optional"
    )
    if segment_rows and not main_rows:
        mode = "segment_only"
    elif main_rows and not segment_rows:
        mode = "main_only"
    elif rows:
        mode = "mixed"
    else:
        mode = "empty"
    return {
        "reference_mode": mode,
        "reference_main_rows": main_rows,
        "reference_segment_rows": segment_rows,
        "reference_total_segment_rows": total_rows,
        "reference_required_segment_rows": required_segment_rows,
        "reference_optional_segment_rows": optional_segment_rows,
    }


def _parse_json_or_literal(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return None


def _mapping_from_value(value: Any) -> dict[str, Any]:
    parsed = _parse_json_or_literal(value)
    return parsed if isinstance(parsed, dict) else {}


def _request_list_from_value(value: Any) -> list[dict[str, Any]]:
    parsed = _parse_json_or_literal(value)
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _llm_request_profile(run_records: list[dict[str, Any]]) -> dict[str, Any]:
    from finengine.skills.pricing import cost_usd

    requests: list[dict[str, Any]] = []
    for record in run_records:
        diag = _mapping_from_value(record.get("_diag") or record.get("diag") or {})
        record_requests = _request_list_from_value(record.get("llm_requests"))
        diag_requests = _request_list_from_value(diag.get("llm_requests"))
        raw_requests = record_requests or diag_requests
        if not raw_requests and diag:
            token_total = sum(int(diag.get(key) or 0) for key in ("tokens_in", "tokens_out", "tokens_cached"))
            if token_total or int(diag.get("llm_calls") or 0) or float(diag.get("cost_usd") or 0.0):
                raw_requests = [
                    {
                        "stage": "main",
                        "model": diag.get("model") or record.get("model_id") or "",
                        "elapsed_s": diag.get("llm_stream_s") or record.get("elapsed_s") or 0,
                        "tokens_in": diag.get("tokens_in") or 0,
                        "tokens_out": diag.get("tokens_out") or 0,
                        "tokens_cached": diag.get("tokens_cached") or 0,
                        "cost_usd": diag.get("cost_usd") or 0.0,
                    }
                ]
        for item in raw_requests:
            model = item.get("model") or record.get("model_id") or diag.get("model") or ""
            tokens_in = int(item.get("tokens_in") or 0)
            tokens_out = int(item.get("tokens_out") or 0)
            tokens_cached = int(item.get("tokens_cached") or 0)
            request_cost = float(item.get("cost_usd") or 0.0)
            if request_cost == 0.0 and model and (tokens_in or tokens_out):
                request_cost = round(cost_usd(model, tokens_in, tokens_out, tokens_cached), 4)
            requests.append(
                {
                    "stage": item.get("stage") or "",
                    "model": model,
                    "elapsed_s": float(item.get("elapsed_s") or 0.0),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "tokens_cached": tokens_cached,
                    "cost_usd": request_cost,
                }
            )
    return {
        "llm_request_count": len(requests),
        "llm_requests_json": json.dumps(requests, ensure_ascii=False),
        "llm_requests_tokens_in": sum(int(item.get("tokens_in") or 0) for item in requests),
        "llm_requests_tokens_out": sum(int(item.get("tokens_out") or 0) for item in requests),
        "llm_requests_tokens_cached": sum(int(item.get("tokens_cached") or 0) for item in requests),
        "llm_requests_elapsed_s": round(sum(float(item.get("elapsed_s") or 0.0) for item in requests), 1),
        "llm_requests_cost_usd": round(sum(float(item.get("cost_usd") or 0.0) for item in requests), 4),
    }


def write_reports(
    *,
    run_dir: Path,
    result: EvaluationResult,
    result_without_unit: EvaluationResult | None = None,
    overall_core_result: EvaluationResult | None = None,
    overall_all_result: EvaluationResult | None = None,
    overall_core_result_without_unit: EvaluationResult | None = None,
    overall_all_result_without_unit: EvaluationResult | None = None,
    segment_core_result: EvaluationResult | None = None,
    segment_all_result: EvaluationResult | None = None,
    segment_core_result_without_unit: EvaluationResult | None = None,
    segment_all_result_without_unit: EvaluationResult | None = None,
    reference_rows: list[SegmentQualityRow],
    prediction_rows: list[SegmentQualityRow],
    ignored_prediction_rows: list[SegmentQualityRow],
    llm_output_rows: list[dict[str, Any]],
    run_context: dict[str, Any],
    run_records: list[dict[str, Any]],
) -> dict[str, Path]:
    files = {
        "summary": run_dir / "metrics_summary.md",
        "by_document": run_dir / "metrics_by_document.csv",
        "by_document_without_unit": run_dir / "metrics_by_document_without_unit.csv",
        "by_enum": run_dir / "metrics_by_enum.csv",
        "by_enum_without_unit": run_dir / "metrics_by_enum_without_unit.csv",
        "segments": run_dir / "metrics_segments.csv",
        "segments_without_unit": run_dir / "metrics_segments_without_unit.csv",
        "row_level": run_dir / "row_level_comparison.csv",
        "errors": run_dir / "errors_top.csv",
        "reference": run_dir / "reference_rows.csv",
        "predictions": run_dir / "predictions_long.csv",
        "ignored_predictions": run_dir / "ignored_predictions_long.csv",
        "llm_output": run_dir / "llm_output_long.csv",
        "llm_io_manifest": run_dir / "llm_io_manifest.csv",
        "run_records": run_dir / "run_records.csv",
        "context": run_dir / "run_context.json",
    }
    metric_headers = list(EvalMetrics(scope="x").to_flat_dict().keys())
    by_document_rows = _metric_rows([*result.by_document.values(), result.macro_by_document])
    by_enum_rows = _metric_rows(result.by_enum.values())
    result_without_unit = result_without_unit or result
    by_document_without_unit_rows = _metric_rows([
        *result_without_unit.by_document.values(),
        result_without_unit.macro_by_document,
    ])
    by_enum_without_unit_rows = _metric_rows(result_without_unit.by_enum.values())
    segment_rows = _metric_rows(
        metric for metric in [
            segment_core_result.micro if segment_core_result else None,
            segment_all_result.micro if segment_all_result else None,
        ]
        if metric is not None
    )
    segment_without_unit_rows = _metric_rows(
        metric for metric in [
            segment_core_result_without_unit.micro if segment_core_result_without_unit else None,
            segment_all_result_without_unit.micro if segment_all_result_without_unit else None,
        ]
        if metric is not None
    )
    row_level = [row.to_dict() for row in result.row_comparisons]
    errors = [row for row in row_level if row["status"] not in {"TP", "TN"}]
    _write_csv(files["by_document"], by_document_rows, metric_headers)
    _write_csv(files["by_document_without_unit"], by_document_without_unit_rows, metric_headers)
    _write_csv(files["by_enum"], by_enum_rows, metric_headers)
    _write_csv(files["by_enum_without_unit"], by_enum_without_unit_rows, metric_headers)
    _write_csv(files["segments"], segment_rows, metric_headers)
    _write_csv(files["segments_without_unit"], segment_without_unit_rows, metric_headers)
    _write_csv(files["row_level"], row_level, list(RowComparison(
        "", "", "", "", "", "", "", "", "", "", "", "", "", "", 0.0
    ).to_dict().keys()))
    _write_csv(files["errors"], errors, list(RowComparison(
        "", "", "", "", "", "", "", "", "", "", "", "", "", "", 0.0
    ).to_dict().keys()))
    row_headers = list(SegmentQualityRow("", "", "", "", "", "").to_dict().keys())
    _write_csv(files["reference"], [row.to_dict() for row in reference_rows], row_headers)
    _write_csv(files["predictions"], [row.to_dict() for row in prediction_rows], row_headers)
    _write_csv(files["ignored_predictions"], [row.to_dict() for row in ignored_prediction_rows], row_headers)
    llm_headers = [
        "dataset",
        "document",
        "enum",
        "period",
        "segment",
        "dimension",
        "value",
        "unit",
        "page",
        "source_text",
        "found",
        "is_calculated",
    ]
    _write_csv(files["llm_output"], llm_output_rows, llm_headers)
    llm_io_rows: list[dict[str, Any]] = []
    llm_io_root = run_dir / "llm_io"
    if llm_io_root.exists():
        for path in sorted(p for p in llm_io_root.rglob("*") if p.is_file()):
            rel = path.relative_to(run_dir)
            parts = rel.parts
            llm_io_rows.append({
                "path": str(rel),
                "dataset": parts[1] if len(parts) > 1 else "",
                "document": parts[2] if len(parts) > 2 else "",
                "file": path.name,
                "bytes": path.stat().st_size,
            })
    _write_csv(
        files["llm_io_manifest"],
        llm_io_rows,
        ["path", "dataset", "document", "file", "bytes"],
    )
    run_record_headers = sorted({key for row in run_records for key in row.keys()}) or ["dataset"]
    _write_csv(files["run_records"], run_records, run_record_headers)
    files["context"].write_text(json.dumps(run_context, ensure_ascii=False, indent=2), encoding="utf-8")

    context_md = "\n".join(f"- **{key}**: `{value}`" for key, value in sorted(run_context.items()))
    top_errors = "\n".join(
        f"- `{row['status']}` {row['document']} | {row['enum']} | {row['period']} | "
        f"{row['segment']}: ref={row['ref_value']} pred={row['pred_value']} ({row['reasons']})"
        for row in errors[:40]
    ) or "No errors."
    llm_io_summary = (
        f"Saved {len(llm_io_rows)} prompt/response files. "
        f"See `{files['llm_io_manifest'].name}` and `llm_io/`."
        if llm_io_rows
        else "No prompt/response files were produced for this run."
    )
    summary = [
        "# Finengine E2E Quality Report",
        "",
        "## Run context",
        context_md,
        "",
        "## OVERALL — CORE",
        _format_metrics_line(overall_core_result.micro) if overall_core_result else "No rows.",
        "",
        "## OVERALL — ALL",
        _format_metrics_line(overall_all_result.micro) if overall_all_result else "No rows.",
        "",
        "## OVERALL WITHOUT UNIT — CORE",
        _format_metrics_line(overall_core_result_without_unit.micro) if overall_core_result_without_unit else "No rows.",
        "",
        "## OVERALL WITHOUT UNIT — ALL",
        _format_metrics_line(overall_all_result_without_unit.micro) if overall_all_result_without_unit else "No rows.",
        "",
        "## SEGMENTS — CORE",
        _format_metrics_line(segment_core_result.micro) if segment_core_result else "No segment rows.",
        "",
        "## SEGMENTS — ALL",
        _format_metrics_line(segment_all_result.micro) if segment_all_result else "No segment rows.",
        "",
        "## SEGMENTS WITHOUT UNIT — CORE",
        _format_metrics_line(segment_core_result_without_unit.micro) if segment_core_result_without_unit else "No segment rows.",
        "",
        "## SEGMENTS WITHOUT UNIT — ALL",
        _format_metrics_line(segment_all_result_without_unit.micro) if segment_all_result_without_unit else "No segment rows.",
        "",
        "## Metrics by document",
        *[f"- {_format_metrics_line(metric)}" for metric in result.by_document.values()],
        "",
        "## Metrics by document without unit",
        *[f"- {_format_metrics_line(metric)}" for metric in result_without_unit.by_document.values()],
        "",
        "## LLM I/O",
        llm_io_summary,
        "",
        "## Top errors",
        top_errors,
        "",
    ]
    files["summary"].write_text("\n".join(summary), encoding="utf-8")
    return files


async def run_finengine_e2e_quality(
    *,
    dataset: str = "both",
    pdf_filters: list[str] | None = None,
    gt_kind: GroundTruthKind | str = GroundTruthKind.SEGMENT_ONLY,
    gt_xlsx: Path | None = None,
    json_dirs: list[Path] | None = None,
    json_paths: list[Path] | None = None,
    pdf_dirs: list[Path] | None = None,
    pdf_paths: list[Path] | None = None,
    input_mode: str = "pdf",
    dataset_name: str | None = None,
    gt_sheet: str | None = None,
    mock_predictions: Path | None = None,
    allow_llm: bool = False,
    include_segments: bool = True,
    include_consolidated_group: bool = False,
    model_id: str | None = None,
    limit: int | None = None,
    max_docs: int = 0,
    out_dir: Path = DEFAULT_RUNS_DIR,
    metrics_from_gt: bool = False,
    metric_scope: str | None = None,
    score_metric_scope: str | None = None,
    real_run_attempts: int = 1,
    extractor_matrix: bool = False,
    company_type: str = "non_financial",
    document_type: str = "financial_report",
    role: str | None = None,
    exclude_us_gaap_nf_weak_metrics: bool = False,
    run_name: str = "finengine_e2e",
    replay_events: Path | None = None,
    normalization: NormalizationMode | str = NormalizationMode.NUMERIC,
    tolerance: float = 0.05,
    business_alpha: float = NUMERIC_ONLY_UNIT_MISMATCH_SCORE,
    require_unit: bool = False,
    unit_mode: UnitMode | str = UnitMode.AUTO,
    prediction_scope: PredictionScope | str = PredictionScope.REFERENCE_ENUMS,
    period_key_mode: PeriodKeyMode | str = PeriodKeyMode.STRICT,
    legacy_replay: bool = False,
    segment_mode: SegmentMode | str = SegmentMode.EXTENDED,
    segment_context_mode: str = "full",
    prompt_text_mode: str = "default",
    force_idp_ocr: bool = False,
    table_context_lines: int = 2,
    ocr_json_variant: str = "old",
    jobs: int = 1,
    openrouter_env_fallback: Path | None = DEFAULT_OPENROUTER_ENV_FALLBACK,
    prefer_openrouter_env_fallback: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> SegmentQualityRun:
    def _progress(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    _progress("normalizing run options")
    segment_mode = SegmentMode.parse(str(segment_mode))
    prompt_text_mode = _normalize_prompt_text_mode(prompt_text_mode)
    table_context_lines = _clamp_table_context_lines(table_context_lines)
    ocr_json_variant = _normalize_ocr_json_variant(ocr_json_variant)
    requested_jobs = max(1, int(jobs or 1))
    effective_jobs = requested_jobs if str(input_mode or "pdf").strip().lower() == "json" else 1
    effective_model_id = model_id or FRONTEND_DEFAULT_MODEL_ID
    _progress("loading benchmark inputs and ground truth")
    dataset_items = dataset_inputs(
        dataset,
        gt_kind=gt_kind,
        gt_xlsx=gt_xlsx,
        json_dirs=json_dirs,
        json_paths=json_paths,
        pdf_dirs=pdf_dirs,
        pdf_paths=pdf_paths,
        input_mode=input_mode,
        ocr_json_variant=ocr_json_variant,
        dataset_name=dataset_name,
        gt_sheet=gt_sheet,
    )
    pdf_filter = resolve_pdf_filter(dataset_items, pdf_filters or [], include_segments)
    run_dir = make_run_dir(
        out_dir,
        run_name,
        pdf_filters=pdf_filters,
        model_id=effective_model_id,
    )
    _progress(f"run directory prepared: {run_dir.name}")
    openrouter_key_info: dict[str, str | bool] = {
        "loaded": False,
        "source": "not_used",
        "path": str(openrouter_env_fallback or ""),
    }

    if allow_llm and not mock_predictions and replay_events is None:
        _progress("checking OpenRouter credentials")
        openrouter_key_info = load_openrouter_key_fallback(
            openrouter_env_fallback,
            prefer_fallback=prefer_openrouter_env_fallback,
        )
        if not settings.openrouter_api_key:
            checked = openrouter_key_info.get("path") or str(DEFAULT_OPENROUTER_ENV_FALLBACK)
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Checked environment/.env and "
                f"fallback env file: {checked}"
            )

    reference_rows: list[SegmentQualityRow] = []
    prediction_rows: list[SegmentQualityRow] = []
    llm_output_rows: list[dict[str, Any]] = []
    run_records: list[dict[str, Any]] = []
    extraction_metric_enums_used: set[str] = set()
    metric_scope_exclusions = _metric_scope_exclusions(
        company_type=company_type,
        exclude_us_gaap_nf_weak_metrics=exclude_us_gaap_nf_weak_metrics,
    )
    metric_scope_inclusions = _metric_scope_inclusions(metric_scope)
    score_metric_scope_requested = score_metric_scope if score_metric_scope is not None else metric_scope
    score_metric_scope_inclusions = _metric_scope_inclusions(score_metric_scope_requested)
    effective_score_metric_scope_labels: set[str] = set()
    effective_score_metric_enums_used: set[str] = set()
    for dataset_item in dataset_items:
        _progress(f"{dataset_item.name}: loading GT rows and resolving metric scope")
        role_extraction_enums, role_output_enums = _role_metric_sets(
            role,
            company_type,
            dataset_name=dataset_item.name,
        )
        item_score_metric_scope_inclusions = score_metric_scope_inclusions
        item_score_metric_scope_label = str(score_metric_scope_requested or "full")
        if (
            item_score_metric_scope_inclusions is None
            and score_metric_scope is None
            and not metric_scope_inclusions
            and role_output_enums is not None
        ):
            item_score_metric_scope_inclusions = role_output_enums
            item_score_metric_scope_label = f"role:{role}:output"
        effective_score_metric_scope_labels.add(item_score_metric_scope_label)
        if item_score_metric_scope_inclusions:
            effective_score_metric_enums_used.update(item_score_metric_scope_inclusions)
        refs = load_reference_rows(
            dataset_item.gt_xlsx,
            dataset_item.name,
            pdf_filter=pdf_filter,
            include_segments=include_segments,
            sheet_name=dataset_item.gt_sheet,
            legacy_replay=legacy_replay,
            segment_mode=SegmentMode.EXTENDED,
        )
        refs = _filter_main_rows_by_metric_scope(refs, item_score_metric_scope_inclusions)
        if metric_scope_exclusions:
            refs = [
                row for row in refs
                if _canon_enum(row.enum) not in metric_scope_exclusions
            ]
        reference_rows.extend(refs)
        if not refs:
            run_records.append(
                {
                    "dataset": dataset_item.name,
                    "mode": "skip",
                    "reason": "no_reference_rows_for_filter",
                    "reference_rows": 0,
                    "prediction_rows": 0,
                }
            )
            continue
        include_main_predictions = (not include_segments) or any(
            not _canon_segment(row.segment) for row in refs
        )
        effective_prompt_segment_mode = (
            segment_mode
            if any(_canon_segment_type(row.segment_type) for row in refs)
            else SegmentMode.EXTENDED
        )
        target_metric_enums = None
        if metric_scope_inclusions:
            target_metric_enums = set(metric_scope_inclusions)
        elif metrics_from_gt:
            target_metric_enums = {_canon_enum(row.enum) for row in refs if _canon_enum(row.enum)}
        elif role_extraction_enums is not None:
            target_metric_enums = set(role_extraction_enums)
        elif not mock_predictions and replay_events is None:
            target_metric_enums = _full_extractable_metric_enums(company_type)
        if target_metric_enums:
            target_metric_enums -= metric_scope_exclusions
            extraction_metric_enums_used.update(target_metric_enums)

        if mock_predictions:
            preds = load_mock_prediction_rows(
                mock_predictions,
                default_dataset=dataset_item.name,
                pdf_filter=pdf_filter,
                include_segments=include_segments,
                include_main_rows=include_main_predictions,
                legacy_replay=legacy_replay,
            )
            prediction_rows.extend([row for row in preds if row.dataset == dataset_item.name])
            run_records.append(
                {
                    "dataset": dataset_item.name,
                    "mode": "mock",
                    "mock_predictions": str(mock_predictions),
                    "reference_rows": len(refs),
                    "prediction_rows": len(preds),
                    "target_metric_enums": len(target_metric_enums or []),
                }
            )
            continue
        if replay_events is not None:
            run_pdf_filter = pdf_filter or {row.document for row in refs}
            selected_json_inputs = _json_inputs_for_docs(dataset_item, run_pdf_filter)
            if max_docs > 0:
                selected_json_inputs = selected_json_inputs[:max_docs]
            if not selected_json_inputs:
                available = ", ".join(_input_pdf_name(path) for path in dataset_item.json_paths[:20])
                requested = ", ".join(sorted(run_pdf_filter))
                raise ValueError(
                    f"No OCR JSON inputs matched selected GT documents: {requested}. "
                    f"Available OCR document names: {available}"
                )
            for json_path, document_name_override in selected_json_inputs:
                filename = document_name_override or _input_pdf_name(json_path)
                event_path = _event_cache_file(replay_events, dataset_item.name, filename)
                rows, table_rows, meta = _read_event_cache(
                    dataset=dataset_item.name,
                    event_path=event_path,
                    default_document=filename,
                    include_segments=include_segments,
                    include_main_rows=include_main_predictions,
                    legacy_replay=legacy_replay,
                )
                prediction_rows.extend(rows)
                llm_output_rows.extend(table_rows)
                run_records.append(
                    {
                        "dataset": dataset_item.name,
                        "mode": "replay",
                        "input": json_path.name,
                        "input_mode": dataset_item.input_mode,
                        "pdf_name": filename,
                        "prediction_rows": len(rows),
                        "target_metric_enums": len(target_metric_enums or []),
                        **meta,
                    }
                )
            continue
        if not allow_llm:
            raise RuntimeError(
                "Real Finengine E2E requires --allow-llm. Use --mock-predictions or --replay-events for no-token tests."
            )
        # Avoid token spend on JSON documents that are present in the folder but
        # absent from the selected GT workbook.
        run_pdf_filter = pdf_filter or {row.document for row in refs}
        selected_json_inputs = _json_inputs_for_docs(dataset_item, run_pdf_filter)
        if max_docs > 0:
            selected_json_inputs = selected_json_inputs[:max_docs]
        if not selected_json_inputs:
            available = ", ".join(_input_pdf_name(path) for path in dataset_item.json_paths[:20])
            requested = ", ".join(sorted(run_pdf_filter))
            raise ValueError(
                f"No OCR JSON inputs matched selected GT documents: {requested}. "
                f"Available OCR document names: {available}"
            )
        _progress(
            f"{dataset_item.name}: selected {len(selected_json_inputs)} input(s); "
            f"mode={dataset_item.input_mode}; jobs={effective_jobs}"
        )
        async def _run_selected_input(
            json_path: Path,
            document_name_override: str | None,
            *,
            docs_dir_override: Path | None = None,
        ) -> tuple[list[SegmentQualityRow], list[dict[str, Any]], dict[str, Any]]:
            attempts = max(1, int(real_run_attempts or 1))
            attempt_history: list[dict[str, Any]] = []
            rows: list[SegmentQualityRow] = []
            table_rows: list[dict[str, Any]] = []
            meta: dict[str, Any] = {}
            for attempt_idx in range(1, attempts + 1):
                filename = document_name_override or _input_pdf_name(json_path)

                def _input_progress(message: str, *, _filename: str = filename, _attempt: int = attempt_idx) -> None:
                    _progress(f"{_filename}: attempt {_attempt}/{attempts}: {message}")

                rows, table_rows, meta = await _run_real_one(
                    dataset=dataset_item.name,
                    json_path=json_path,
                    run_dir=run_dir,
                    model_id=effective_model_id,
                    limit=limit,
                    include_segments=include_segments,
                    include_main_rows=include_main_predictions,
                    include_consolidated_group=include_consolidated_group,
                    metric_enums=target_metric_enums,
                    company_type_override=company_type,
                    document_type_override=document_type,
                    role=role,
                    extractor_matrix=extractor_matrix,
                    legacy_replay=legacy_replay,
                    segment_mode=effective_prompt_segment_mode,
                    segment_context_mode=segment_context_mode,
                    prompt_text_mode=prompt_text_mode,
                    force_idp_ocr=force_idp_ocr,
                    table_context_lines=table_context_lines,
                    document_name_override=document_name_override,
                    docs_dir_override=docs_dir_override,
                    progress_callback=_input_progress,
                )
                attempt_history.append(
                    {
                        "attempt": attempt_idx,
                        "prediction_rows": len(rows),
                        "error": meta.get("error", ""),
                        "elapsed_s": meta.get("elapsed_s", ""),
                    }
                )
                if len(rows) > 0 or not _is_retryable_real_run_error(meta) or attempt_idx >= attempts:
                    break
                await asyncio.sleep(min(30, 5 * attempt_idx))
            meta = dict(meta)
            meta["attempt"] = len(attempt_history)
            meta["attempts"] = attempts
            meta["attempt_history"] = json.dumps(attempt_history, ensure_ascii=False)
            return (
                rows,
                table_rows,
                {
                    "dataset": dataset_item.name,
                    "mode": "real",
                    "input": json_path.name,
                    "input_mode": dataset_item.input_mode,
                    "pdf_name": document_name_override or _input_pdf_name(json_path),
                    "prediction_rows": len(rows),
                    "target_metric_enums": len(target_metric_enums or []),
                    **meta,
                },
            )

        if effective_jobs > 1 and dataset_item.input_mode == "json":
            old_docs_dir = settings.docs_dir
            with tempfile.TemporaryDirectory(prefix="finengine-e2e-batch-") as tmp:
                shared_docs_dir = Path(tmp)
                settings.docs_dir = shared_docs_dir
                sem = asyncio.Semaphore(effective_jobs)

                async def _worker(item: tuple[Path, str | None]):
                    async with sem:
                        return await _run_selected_input(
                            item[0],
                            item[1],
                            docs_dir_override=shared_docs_dir,
                        )

                try:
                    real_results = await asyncio.gather(*[_worker(item) for item in selected_json_inputs])
                finally:
                    settings.docs_dir = old_docs_dir
        else:
            real_results = [
                await _run_selected_input(json_path, document_name_override)
                for json_path, document_name_override in selected_json_inputs
            ]

        for rows, table_rows, record in real_results:
            prediction_rows.extend(rows)
            llm_output_rows.extend(table_rows)
            run_records.append(record)

    _progress("evaluating predictions against GT")
    docs_with_predictions = {_canon_pdf(row.document) for row in prediction_rows}
    if docs_with_predictions:
        reference_rows = [row for row in reference_rows if _canon_pdf(row.document) in docs_with_predictions]
    if not reference_rows:
        raise ValueError("No reference rows selected for this run.")

    reference_rows, ignored_calculated_reference_rows = filter_calculated_rows(reference_rows)
    prediction_rows, ignored_calculated_prediction_rows = filter_calculated_rows(prediction_rows)
    if not reference_rows:
        raise ValueError("No non-calculated reference rows selected for this run.")
    reference_rows, ignored_total_segment_reference_rows = filter_total_segment_rows(reference_rows)
    prediction_rows, ignored_total_segment_prediction_rows = filter_total_segment_rows(prediction_rows)
    if not reference_rows:
        raise ValueError("No non-TOTAL reference rows selected for this run.")
    all_scored_reference_rows = list(reference_rows)
    all_scored_prediction_rows = list(prediction_rows)
    reference_rows, ignored_optional_segment_reference_rows = (
        filter_optional_segment_rows(reference_rows)
        if include_segments and segment_mode == SegmentMode.CORE
        else (reference_rows, [])
    )
    if not reference_rows:
        raise ValueError("No non-optional reference rows selected for this run.")

    prediction_rows, ignored_prediction_rows = _filter_predictions_by_reference_scope(
        prediction_rows,
        reference_rows,
        prediction_scope,
    )
    ignored_prediction_rows = [
        *ignored_prediction_rows,
        *ignored_calculated_prediction_rows,
        *ignored_total_segment_prediction_rows,
    ]
    resolved_require_unit = _resolve_require_unit(reference_rows, unit_mode, require_unit)
    result = evaluate_rows(
        reference_rows,
        prediction_rows,
        normalization_mode=normalization,
        tolerance=tolerance,
        business_alpha=business_alpha,
        require_unit=resolved_require_unit,
        period_key_mode=period_key_mode,
    )
    result_without_unit = evaluate_rows(
        reference_rows,
        prediction_rows,
        normalization_mode=normalization,
        tolerance=tolerance,
        business_alpha=business_alpha,
        require_unit=False,
        period_key_mode=period_key_mode,
    )
    main_reference_rows = [row for row in all_scored_reference_rows if not _canon_segment(row.segment)]
    main_prediction_rows = _filter_predictions_by_reference_scope(
        [row for row in all_scored_prediction_rows if not _canon_segment(row.segment)],
        main_reference_rows,
        prediction_scope,
    )[0] if main_reference_rows else []
    main_result = (
        evaluate_rows(
            main_reference_rows,
            main_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=resolved_require_unit,
            period_key_mode=period_key_mode,
        )
        if main_reference_rows else None
    )
    if main_result:
        main_result.micro.scope = "main"
    segment_core_reference_rows = _segment_scope_rows(all_scored_reference_rows, include_optional=False)
    segment_all_reference_rows = _segment_scope_rows(all_scored_reference_rows, include_optional=True)
    segment_scored_prediction_rows = _segment_scope_rows(all_scored_prediction_rows, include_optional=True)
    segment_core_prediction_rows = _filter_predictions_by_reference_scope(
        _segment_scope_rows(segment_scored_prediction_rows, include_optional=False),
        segment_core_reference_rows,
        prediction_scope,
    )[0] if segment_core_reference_rows else []
    segment_all_prediction_rows = _filter_predictions_by_reference_scope(
        segment_scored_prediction_rows,
        segment_all_reference_rows,
        prediction_scope,
    )[0] if segment_all_reference_rows else []
    segment_core_result = (
        evaluate_rows(
            segment_core_reference_rows,
            segment_core_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=resolved_require_unit,
            period_key_mode=period_key_mode,
        )
        if segment_core_reference_rows else None
    )
    segment_all_result = (
        evaluate_rows(
            segment_all_reference_rows,
            segment_all_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=resolved_require_unit,
            period_key_mode=period_key_mode,
        )
        if segment_all_reference_rows else None
    )
    segment_core_result_without_unit = (
        evaluate_rows(
            segment_core_reference_rows,
            segment_core_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=False,
            period_key_mode=period_key_mode,
        )
        if segment_core_reference_rows else None
    )
    segment_all_result_without_unit = (
        evaluate_rows(
            segment_all_reference_rows,
            segment_all_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=False,
            period_key_mode=period_key_mode,
        )
        if segment_all_reference_rows else None
    )
    overall_core_reference_rows = [*main_reference_rows, *segment_core_reference_rows]
    overall_core_prediction_rows = _filter_predictions_by_reference_scope(
        [*main_prediction_rows, *segment_core_prediction_rows],
        overall_core_reference_rows,
        prediction_scope,
    )[0] if overall_core_reference_rows else []
    overall_all_reference_rows = [*main_reference_rows, *segment_all_reference_rows]
    overall_all_prediction_rows = _filter_predictions_by_reference_scope(
        [*main_prediction_rows, *segment_all_prediction_rows],
        overall_all_reference_rows,
        prediction_scope,
    )[0] if overall_all_reference_rows else []
    overall_core_result = (
        evaluate_rows(
            overall_core_reference_rows,
            overall_core_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=resolved_require_unit,
            period_key_mode=period_key_mode,
        )
        if overall_core_reference_rows else None
    )
    overall_all_result = (
        evaluate_rows(
            overall_all_reference_rows,
            overall_all_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=resolved_require_unit,
            period_key_mode=period_key_mode,
        )
        if overall_all_reference_rows else None
    )
    overall_core_result_without_unit = (
        evaluate_rows(
            overall_core_reference_rows,
            overall_core_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=False,
            period_key_mode=period_key_mode,
        )
        if overall_core_reference_rows else None
    )
    overall_all_result_without_unit = (
        evaluate_rows(
            overall_all_reference_rows,
            overall_all_prediction_rows,
            normalization_mode=normalization,
            tolerance=tolerance,
            business_alpha=business_alpha,
            require_unit=False,
            period_key_mode=period_key_mode,
        )
        if overall_all_reference_rows else None
    )
    if overall_core_result:
        overall_core_result.micro.scope = "overall_core"
    if overall_all_result:
        overall_all_result.micro.scope = "overall_all"
    if overall_core_result_without_unit:
        overall_core_result_without_unit.micro.scope = "overall_core_without_unit"
    if overall_all_result_without_unit:
        overall_all_result_without_unit.micro.scope = "overall_all_without_unit"
    if segment_core_result:
        segment_core_result.micro.scope = "segment_core"
    if segment_all_result:
        segment_all_result.micro.scope = "segment_all"
    if segment_core_result_without_unit:
        segment_core_result_without_unit.micro.scope = "segment_core_without_unit"
    if segment_all_result_without_unit:
        segment_all_result_without_unit.micro.scope = "segment_all_without_unit"
    run_context = {
        "pipeline_profile": "finengine_dev_registry",
        "frontend_equivalent": str(
            input_mode == "pdf" and not metrics_from_gt and mock_predictions is None and replay_events is None
        ),
        "dataset": dataset,
        "gt_kind": GroundTruthKind.parse(str(gt_kind)).value,
        "dataset_items": ",".join(item.name for item in dataset_items),
        "gt_xlsx": ";".join(str(item.gt_xlsx) for item in dataset_items),
        "gt_sheet": gt_sheet or "",
        "input_mode": input_mode,
        "prompt_text_mode": prompt_text_mode,
        "force_idp_ocr": str(force_idp_ocr),
        "table_context_lines": str(table_context_lines),
        "ocr_json_variant": ocr_json_variant,
        "jobs_requested": str(requested_jobs),
        "jobs_effective": str(effective_jobs),
        "inputs": ";".join(str(path) for item in dataset_items for path in item.json_paths),
        "input_paths": ";".join(str(path) for item in dataset_items for path in item.json_paths),
        "pdf_filters": ",".join(pdf_filters or []) or "<all>",
        "mock_predictions": str(mock_predictions or ""),
        "allow_llm": str(allow_llm),
        "include_segments": str(include_segments),
        "include_consolidated_group": str(include_consolidated_group),
        "segment_mode": segment_mode.value,
        "segment_context_mode": segment_context_mode,
        "replay_events": str(replay_events or ""),
        "real_run_attempts": str(max(1, int(real_run_attempts or 1))),
        "metrics_from_gt": str(metrics_from_gt),
        "metric_scope": str(metric_scope or "full"),
        "metric_scope_inclusions": ",".join(sorted(metric_scope_inclusions or [])),
        "score_metric_scope": ",".join(sorted(effective_score_metric_scope_labels)) or "full",
        "score_metric_scope_inclusions": ",".join(sorted(effective_score_metric_enums_used)),
        "extractor_matrix": str(extractor_matrix),
        "document_type": document_type,
        "company_type": company_type,
        "role": role or "",
        "exclude_us_gaap_nf_weak_metrics": str(exclude_us_gaap_nf_weak_metrics),
        "metric_scope_exclusions": ",".join(sorted(metric_scope_exclusions)),
        "target_metric_enums": ",".join(
            sorted({_canon_enum(row.enum) for row in reference_rows if _canon_enum(row.enum)})
        ),
        "target_metric_enum_count": str(
            len({_canon_enum(row.enum) for row in reference_rows if _canon_enum(row.enum)})
        ),
        "extraction_metric_enums": ",".join(sorted(extraction_metric_enums_used)),
        "extraction_metric_enum_count": str(len(extraction_metric_enums_used)),
        "extraction_metric_scope": (
            str(metric_scope)
            if metric_scope_inclusions
            else ("gt" if metrics_from_gt else ("full_registry" if extraction_metric_enums_used else "replay_or_mock"))
        ),
        "model_id": effective_model_id,
        "model_id_requested": model_id or "",
        "limit": str(limit or ""),
        "max_docs": str(max_docs),
        "normalization": NormalizationMode.parse(str(normalization)).value,
        "tolerance": str(tolerance),
        "business_alpha": str(business_alpha),
        "unit_mode": UnitMode.parse(str(unit_mode)).value,
        "prediction_scope": PredictionScope.parse(str(prediction_scope)).value,
        "period_key_mode": PeriodKeyMode.parse(str(period_key_mode)).value,
        "legacy_replay": str(legacy_replay),
        "require_unit": str(resolved_require_unit),
        "legacy_require_unit_flag": str(require_unit),
        "reference_rows": str(len(reference_rows)),
        "prediction_rows": str(len(prediction_rows)),
        "ignored_prediction_rows": str(len(ignored_prediction_rows)),
        "ignored_calculated_reference_rows": str(len(ignored_calculated_reference_rows)),
        "ignored_calculated_prediction_rows": str(len(ignored_calculated_prediction_rows)),
        "ignored_total_segment_reference_rows": str(len(ignored_total_segment_reference_rows)),
        "ignored_total_segment_prediction_rows": str(len(ignored_total_segment_prediction_rows)),
        "ignored_optional_segment_reference_rows": str(len(ignored_optional_segment_reference_rows)),
        "openrouter_key_source": str(openrouter_key_info.get("source", "")),
        "openrouter_key_fallback_path": str(openrouter_key_info.get("path", "")),
        "openrouter_key_loaded_from_fallback": str(openrouter_key_info.get("loaded", False)),
        "prefer_openrouter_env_fallback": str(prefer_openrouter_env_fallback),
    }
    run_context.update({key: str(value) for key, value in _reference_profile(reference_rows).items()})
    run_context.update({key: str(value) for key, value in _llm_request_profile(run_records).items()})
    _progress("writing run artifacts")
    files = write_reports(
        run_dir=run_dir,
        result=result,
        result_without_unit=result_without_unit,
        overall_core_result=overall_core_result,
        overall_all_result=overall_all_result,
        overall_core_result_without_unit=overall_core_result_without_unit,
        overall_all_result_without_unit=overall_all_result_without_unit,
        segment_core_result=segment_core_result,
        segment_all_result=segment_all_result,
        segment_core_result_without_unit=segment_core_result_without_unit,
        segment_all_result_without_unit=segment_all_result_without_unit,
        reference_rows=reference_rows,
        prediction_rows=prediction_rows,
        ignored_prediction_rows=ignored_prediction_rows,
        llm_output_rows=llm_output_rows,
        run_context=run_context,
        run_records=run_records,
    )
    return SegmentQualityRun(
        run_dir=run_dir,
        result=result,
        reference_rows=reference_rows,
        prediction_rows=prediction_rows,
        files=files,
        run_records=run_records,
        llm_output_rows=llm_output_rows,
        overall_core_result=overall_core_result,
        overall_all_result=overall_all_result,
        main_result=main_result,
        result_without_unit=result_without_unit,
        overall_core_result_without_unit=overall_core_result_without_unit,
        overall_all_result_without_unit=overall_all_result_without_unit,
        segment_core_result=segment_core_result,
        segment_all_result=segment_all_result,
        segment_core_result_without_unit=segment_core_result_without_unit,
        segment_all_result_without_unit=segment_all_result_without_unit,
    )

def _parse_pdf_args(raw: list[str]) -> list[str]:
    out: list[str] = []
    for item in raw:
        out.extend(part.strip() for part in item.split(",") if part.strip())
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Finengine extraction E2E quality tests.")
    parser.add_argument("--dataset", choices=["ifrs", "usgaap", "ifrsbank", "both", "all", "custom"], default="both")
    parser.add_argument("--dataset-name", default=None, help="Dataset label written to reports for custom inputs")
    parser.add_argument(
        "--gt-kind",
        choices=[m.value for m in GroundTruthKind],
        default=GroundTruthKind.SEGMENT_ONLY.value,
        help="Built-in ground-truth preset for non-custom datasets",
    )
    parser.add_argument("--gt-xlsx", type=Path, default=None, help="Ground-truth workbook path")
    parser.add_argument("--gt-sheet", default=None, help="Ground-truth sheet name; defaults to Лист3 or first sheet")
    parser.add_argument(
        "--input-mode",
        choices=["json", "pdf"],
        default="pdf",
        help="pdf runs the frontend-equivalent IDP pipeline; json is a faster post-OCR fixture mode",
    )
    parser.add_argument(
        "--prompt-text-mode",
        choices=sorted(PROMPT_TEXT_MODES),
        default="default",
        help=(
            "Experimental prompt-input assembly from OCR: default keeps production-like OCR text, "
            "duplicate_tables appends table blocks again, no_tables strips/omits table blocks "
            "(text_only is a backward-compatible alias), tables_context sends text plus table blocks "
            "with neighbor context."
        ),
    )
    parser.add_argument(
        "--force-idp-ocr",
        action="store_true",
        help="For PDF inputs, skip sibling flat OCR JSON fallback and use/reuse raw IDP OCR cache.",
    )
    parser.add_argument(
        "--table-context-lines",
        type=int,
        default=2,
        help="Number of non-table lines before/after each table for --prompt-text-mode tables_context.",
    )
    parser.add_argument(
        "--ocr-json-variant",
        choices=sorted(OCR_JSON_VARIANTS),
        default="old",
        help="For --input-mode json, choose sibling OCR fixtures: old excludes *_new.json, new keeps only *_new.json, both keeps all.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="Max concurrent documents for --input-mode json. PDF input is always forced to one job to avoid OCR overload.",
    )
    parser.add_argument("--json-dir", type=Path, action="append", default=[], help="Folder with OCR JSON files")
    parser.add_argument("--json-path", type=Path, action="append", default=[], help="Single OCR JSON file or folder")
    parser.add_argument("--pdf-dir", type=Path, action="append", default=[], help="Folder with PDF files for --input-mode pdf")
    parser.add_argument("--pdf-path", type=Path, action="append", default=[], help="Single PDF file or folder for --input-mode pdf")
    parser.add_argument("--pdf", action="append", default=[], help="PDF filename or substring")
    parser.add_argument("--mock-predictions", type=Path, default=None)
    parser.add_argument("--allow-llm", action="store_true", help="Actually call the LLM backend")
    segment_group = parser.add_mutually_exclusive_group()
    segment_group.add_argument(
        "--include-segments",
        dest="include_segments",
        action="store_true",
        default=True,
        help="Run the second-stage segment extraction step (default)",
    )
    segment_group.add_argument(
        "--no-segments",
        dest="include_segments",
        action="store_false",
        help="Run only the main metric extraction step",
    )
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--limit", type=int, default=0, help="Limit base registry metrics for smoke runs")
    parser.add_argument("--max-docs", type=int, default=0)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--metrics-from-gt", action="store_true", help="Extract only enum set present in the filtered GT rows")
    parser.add_argument(
        "--metric-scope",
        default="full",
        help=(
            "Named main-metric scope or enums:ENUM1,ENUM2 for prompt and, unless --score-metric-scope is set, main-row scoring. "
            "Segment extraction/scoring remains controlled independently by --include-segments/--segment-mode."
        ),
    )
    parser.add_argument(
        "--score-metric-scope",
        default=None,
        help="Optional named main-metric scope or enums:ENUM1,ENUM2 for scoring only; extraction still uses --metric-scope.",
    )
    parser.add_argument(
        "--real-run-attempts",
        type=int,
        default=1,
        help="Retry a real document run on transient transport errors up to N attempts",
    )
    parser.add_argument(
        "--exclude-us-gaap-nf-weak-metrics",
        action="store_true",
        help="Deprecated compatibility flag: NF registries already exclude OTHER_ASSETS and OTHER_LIABILITIES",
    )
    parser.add_argument(
        "--extractor-matrix",
        action="store_true",
        help="Route real runs through the new per-profile extractor matrix instead of direct extract_stream_finengine_dev",
    )
    parser.add_argument("--company-type", choices=["non_financial", "bank", "hybrid", "unknown"], default="non_financial")
    parser.add_argument(
        "--document-type",
        choices=["financial_report", "press_release", "presentation", "equity_research"],
        default="financial_report",
        help="Source type used to select the source-scoped metric registry during real runs",
    )
    parser.add_argument("--role", default=None, help="Extraction role, e.g. other, die, cib, baifp")
    parser.add_argument("--run-name", default="finengine_e2e")
    parser.add_argument(
        "--replay-events",
        type=Path,
        default=None,
        help="Re-score saved raw event JSONL from a prior run/run dir without calling the LLM",
    )
    parser.add_argument("--normalization", choices=["NUMERIC", "STRING"], default="NUMERIC")
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--business-alpha", type=float, default=NUMERIC_ONLY_UNIT_MISMATCH_SCORE)
    parser.add_argument("--require-unit", action="store_true", help="Legacy alias for --unit-mode REQUIRE")
    parser.add_argument(
        "--unit-mode",
        choices=["AUTO", "IGNORE", "REQUIRE"],
        default="AUTO",
        help="AUTO compares units only when GT contains a unit; IGNORE never compares; REQUIRE always compares",
    )
    parser.add_argument(
        "--prediction-scope",
        choices=["REFERENCE_ENUMS", "ALL"],
        default="REFERENCE_ENUMS",
        help="REFERENCE_ENUMS ignores predictions for enum/doc pairs absent from incomplete GT",
    )
    parser.add_argument(
        "--period-key-mode",
        choices=[m.value for m in PeriodKeyMode],
        default=PeriodKeyMode.STRICT.value,
        help="STRICT uses canonical periods; FY_31_DEC_EQUIV treats FY YYYY as equal to 31.12.YYYY",
    )
    parser.add_argument(
        "--segment-mode",
        choices=[m.value for m in SegmentMode],
        default=SegmentMode.EXTENDED.value,
        help="core scores/prompts only operating/product/geography segments; extended also includes cross-breakdowns",
    )
    parser.add_argument(
        "--segment-context-mode",
        choices=["full", "focused"],
        default="full",
        help="full sends the whole OCR text to segment extraction; focused uses the old page/anchor selector",
    )
    parser.add_argument(
        "--legacy-replay",
        action="store_true",
        help=(
            "Use the legacy cached-replay mode: keep GT values verbatim in row outputs "
            "and recover accounting signs from source_text for old events. By default "
            "GT is normalized eagerly and predictions trust period_values.value."
        ),
    )
    parser.add_argument(
        "--openrouter-env-fallback",
        type=Path,
        default=DEFAULT_OPENROUTER_ENV_FALLBACK,
        help="Fallback dotenv file for OPENROUTER_API_KEY when environment/.env are empty",
    )
    parser.add_argument(
        "--prefer-openrouter-env-fallback",
        action="store_true",
        help="Let --openrouter-env-fallback override an already loaded OPENROUTER_API_KEY",
    )
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    run = await run_finengine_e2e_quality(
        dataset=args.dataset,
        pdf_filters=_parse_pdf_args(args.pdf),
        gt_kind=args.gt_kind,
        gt_xlsx=args.gt_xlsx,
        json_dirs=args.json_dir,
        json_paths=args.json_path,
        pdf_dirs=args.pdf_dir,
        pdf_paths=args.pdf_path,
        input_mode=args.input_mode,
        prompt_text_mode=args.prompt_text_mode,
        force_idp_ocr=args.force_idp_ocr,
        table_context_lines=args.table_context_lines,
        ocr_json_variant=args.ocr_json_variant,
        jobs=args.jobs,
        dataset_name=args.dataset_name,
        gt_sheet=args.gt_sheet,
        mock_predictions=args.mock_predictions,
        allow_llm=args.allow_llm,
        include_segments=args.include_segments,
        model_id=args.model_id,
        limit=args.limit or None,
        max_docs=args.max_docs,
        out_dir=args.out_dir,
        metrics_from_gt=args.metrics_from_gt,
        metric_scope=args.metric_scope,
        score_metric_scope=args.score_metric_scope,
        real_run_attempts=args.real_run_attempts,
        extractor_matrix=args.extractor_matrix,
        company_type=args.company_type,
        document_type=args.document_type,
        role=args.role,
        exclude_us_gaap_nf_weak_metrics=args.exclude_us_gaap_nf_weak_metrics,
        run_name=args.run_name,
        replay_events=args.replay_events,
        normalization=args.normalization,
        tolerance=args.tolerance,
        business_alpha=args.business_alpha,
        require_unit=args.require_unit,
        unit_mode=args.unit_mode,
        prediction_scope=args.prediction_scope,
        period_key_mode=args.period_key_mode,
        segment_mode=args.segment_mode,
        segment_context_mode=args.segment_context_mode,
        legacy_replay=args.legacy_replay,
        openrouter_env_fallback=args.openrouter_env_fallback,
        prefer_openrouter_env_fallback=args.prefer_openrouter_env_fallback,
    )
    try:
        from test_benchmark.build_benchmark import write_run_workbooks

        write_run_workbooks([run.run_dir])
    except Exception as exc:
        print(f"Warning: could not write benchmark.xlsx: {exc}")
    print(f"Reports written to {run.run_dir}")
    print("\n".join(_format_cli_summary(run)))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
