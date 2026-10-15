"""Shared benchmark data structures and the retired v1 workbook builder.

New extraction runs use ``benchmark_v2.py`` and golden baskets. The collection
helpers remain here because v2 imports them and historical run artifacts can
still be collected without restoring the deleted legacy fixture tree.

Examples:
  # Rebuild Excel from copied benchmark runs, no LLM calls.
  uv run python tests/test_benchmark/build_benchmark.py --mode collect

  # Rebuild Excel from existing historical E2E runs, no LLM calls.
  uv run python tests/test_benchmark/build_benchmark.py --mode collect \
    --source-runs tests/test_benchmark/runs

  # Run the copied harness for the main quarterly set, then build Excel.
  uv run python tests/test_benchmark/build_benchmark.py --mode run \
    --suite main --allow-llm
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = ROOT / "tests" / "test_benchmark"
DEFAULT_RUNS_DIR = BENCHMARK_ROOT / "runs"
DEFAULT_OUT_XLSX = BENCHMARK_ROOT / "Finengine_E2E_Benchmarks.xlsx"
PER_RUN_XLSX_NAME = "benchmark.xlsx"
BANKS_RU_ANNUAL_DIR = ROOT / "валидация_банки_ру_годовые"
BANKS_RU_ANNUAL_GT = BENCHMARK_ROOT / "data" / "banks_ru_annual" / "gt" / "parameter_page_reference_banks.csv"
BANKS_RU_IFRS_DIR = ROOT / "валидация_банки_мсфо"
BANKS_RU_IFRS_GT = BENCHMARK_ROOT / "data" / "banks_ru_ifrs" / "gt" / "parameter_page_reference_banks_ifrs.csv"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from test_benchmark.finengine_e2e_tests.quality import (  # noqa: E402
    GroundTruthKind,
    OCR_JSON_VARIANTS,
    PROMPT_TEXT_MODES,
    SegmentMode,
    _format_metrics_line,
    run_finengine_e2e_quality,
)


BANK_WORST5_SUPPORTED_SCOPE = (
    "enums:"
    "SECURITIES_LIABILITIES,"
    "PPE,"
    "SECURITIES_ASSETS,"
    "LOANS_STAGE3_GROSS,"
    "NON_INTEREST_INCOME_BANKS"
)
BANK_WORST5_SUPPORTED_DOCS = (
    "Совкомбанк 9м.pdf",
    "Юникредит банк 6м.pdf",
    "Уралсиб 1Q.pdf",
)


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    suite: str
    dataset: str
    gt_kind: str
    company_type: str
    include_segments: bool
    input_mode: str = "pdf"
    dataset_name: str | None = None
    gt_xlsx: Path | None = None
    json_dirs: tuple[Path, ...] = ()
    pdf_dirs: tuple[Path, ...] = ()
    pdf_filters: tuple[str, ...] = ()
    metrics_from_gt: bool = False
    metric_scope: str = "full"
    score_metric_scope: str | None = None
    extractor_matrix: bool = False
    include_consolidated_group: bool = False
    role: str | None = None
    unit_mode: str = "AUTO"
    period_key_mode: str = "STRICT"
    segment_mode: str = SegmentMode.EXTENDED.value
    segment_context_mode: str = "full"
    prediction_scope: str = "REFERENCE_ENUMS"
    repeat: int = 1
    real_run_attempts: int = 1


_RETIRED_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="main_mmk_ifrs",
        suite="main",
        dataset="ifrs",
        gt_kind=GroundTruthKind.MAIN_OLEG.value,
        company_type="non_financial",
        include_segments=True,
        pdf_filters=("mmk.pdf",),
    ),
    BenchmarkCase(
        name="main_ba_intc_usgaap",
        suite="main",
        dataset="usgaap",
        gt_kind=GroundTruthKind.MAIN_OLEG.value,
        company_type="non_financial",
        include_segments=True,
    ),
    BenchmarkCase(
        name="main_vtb_ifrsbank",
        suite="main",
        dataset="ifrsbank",
        gt_kind=GroundTruthKind.MAIN_OLEG.value,
        company_type="bank",
        include_segments=True,
        pdf_filters=("vtb.pdf",),
    ),
    BenchmarkCase(
        name="full_amd_usgaap",
        suite="full",
        dataset="usgaap",
        gt_kind=GroundTruthKind.FULL.value,
        company_type="non_financial",
        include_segments=True,
        pdf_filters=("0000002488-25-000012.pdf",),
    ),
    BenchmarkCase(
        name="full_x5_ifrs",
        suite="full",
        dataset="ifrs",
        gt_kind=GroundTruthKind.FULL.value,
        company_type="non_financial",
        include_segments=True,
        pdf_filters=("ИКС-5 2023.pdf",),
    ),
    BenchmarkCase(
        name="segments_ifrs",
        suite="segments",
        dataset="ifrs",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        company_type="non_financial",
        include_segments=True,
    ),
    BenchmarkCase(
        name="segments_usgaap",
        suite="segments",
        dataset="usgaap",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        company_type="non_financial",
        include_segments=True,
    ),
    BenchmarkCase(
        name="banks_ru_annual",
        suite="banks_ru",
        dataset="custom",
        dataset_name="BANKS_RU_ANNUAL",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        gt_xlsx=BANKS_RU_ANNUAL_GT,
        json_dirs=(BANKS_RU_ANNUAL_DIR,),
        pdf_dirs=(BANKS_RU_ANNUAL_DIR,),
        company_type="bank",
        include_segments=False,
        extractor_matrix=True,
        include_consolidated_group=True,
        role="other",
        input_mode="pdf",
        period_key_mode="FY_31_DEC_EQUIV",
    ),
    BenchmarkCase(
        name="banks_ru_annual_mib_other_die_score",
        suite="banks_ru",
        dataset="custom",
        dataset_name="BANKS_RU_ANNUAL",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        gt_xlsx=BANKS_RU_ANNUAL_GT,
        json_dirs=(BANKS_RU_ANNUAL_DIR,),
        pdf_dirs=(BANKS_RU_ANNUAL_DIR,),
        pdf_filters=("mib-2024-godovaya.pdf",),
        company_type="bank",
        include_segments=False,
        extractor_matrix=True,
        include_consolidated_group=True,
        role="other",
        input_mode="json",
        period_key_mode="FY_31_DEC_EQUIV",
        score_metric_scope="role_die_bank_output",
        real_run_attempts=2,
    ),
    BenchmarkCase(
        name="banks_ru_annual_mib_die",
        suite="banks_ru",
        dataset="custom",
        dataset_name="BANKS_RU_ANNUAL",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        gt_xlsx=BANKS_RU_ANNUAL_GT,
        json_dirs=(BANKS_RU_ANNUAL_DIR,),
        pdf_dirs=(BANKS_RU_ANNUAL_DIR,),
        pdf_filters=("mib-2024-godovaya.pdf",),
        company_type="bank",
        include_segments=False,
        extractor_matrix=True,
        role="die",
        input_mode="json",
        period_key_mode="FY_31_DEC_EQUIV",
        metric_scope="role_die_bank_extraction",
        score_metric_scope="role_die_bank_output",
        real_run_attempts=2,
    ),
    BenchmarkCase(
        name="banks_ru_ifrs",
        suite="banks_ru",
        dataset="custom",
        dataset_name="BANKS_RU_IFRS",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        gt_xlsx=BANKS_RU_IFRS_GT,
        json_dirs=(BANKS_RU_IFRS_DIR,),
        pdf_dirs=(BANKS_RU_IFRS_DIR,),
        company_type="bank",
        include_segments=False,
        extractor_matrix=True,
        include_consolidated_group=True,
        role="other",
        input_mode="pdf",
        period_key_mode="FY_31_DEC_EQUIV",
    ),
    BenchmarkCase(
        name="banks_ru_ifrs_worst5_full_score",
        suite="banks_ru",
        dataset="custom",
        dataset_name="BANKS_RU_IFRS",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        gt_xlsx=BANKS_RU_IFRS_GT,
        json_dirs=(BANKS_RU_IFRS_DIR,),
        pdf_dirs=(BANKS_RU_IFRS_DIR,),
        pdf_filters=BANK_WORST5_SUPPORTED_DOCS,
        company_type="bank",
        include_segments=False,
        extractor_matrix=True,
        include_consolidated_group=True,
        role="other",
        input_mode="json",
        period_key_mode="FY_31_DEC_EQUIV",
        score_metric_scope=BANK_WORST5_SUPPORTED_SCOPE,
        real_run_attempts=2,
    ),
    BenchmarkCase(
        name="banks_ru_ifrs_worst5_narrow",
        suite="banks_ru",
        dataset="custom",
        dataset_name="BANKS_RU_IFRS",
        gt_kind=GroundTruthKind.SEGMENT_ONLY.value,
        gt_xlsx=BANKS_RU_IFRS_GT,
        json_dirs=(BANKS_RU_IFRS_DIR,),
        pdf_dirs=(BANKS_RU_IFRS_DIR,),
        pdf_filters=BANK_WORST5_SUPPORTED_DOCS,
        company_type="bank",
        include_segments=False,
        extractor_matrix=True,
        include_consolidated_group=True,
        role="other",
        input_mode="json",
        period_key_mode="FY_31_DEC_EQUIV",
        metric_scope=BANK_WORST5_SUPPORTED_SCOPE,
        score_metric_scope=BANK_WORST5_SUPPORTED_SCOPE,
        real_run_attempts=2,
    ),
)

# The data-backed v1 cases were retired with the duplicate E2E tree and the
# standalone banks.ru GT. Benchmark v2 builds current cases from golden baskets.
CASES: tuple[BenchmarkCase, ...] = ()


def _num(raw: Any, default: float = 0.0) -> float:
    try:
        if raw is None or raw == "":
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def _int(raw: Any, default: int = 0) -> int:
    try:
        if raw is None or raw == "":
            return default
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _read_context(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run_context.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _latest_run_dirs(roots: list[Path]) -> list[Path]:
    dirs: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir() and (child / "metrics_by_document.csv").exists():
                dirs.append(child)
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)


def _metric_dict(row: dict[str, Any]) -> dict[str, Any]:
    counts = {
        "TP": _num(row.get("TP")),
        "EFP": _int(row.get("EFP")),
        "EFN": _int(row.get("EFN")),
        "EV": _int(row.get("EV")),
        "TN": _int(row.get("TN")),
    }
    return {
        **counts,
        "precision": _num(row.get("precision")),
        "recall": _num(row.get("recall")),
        "f1": _num(row.get("f1")),
        "f05": _num(row.get("f05")),
        "accuracy": _num(row.get("accuracy")),
        "business_precision": _num(row.get("business_precision")),
        "business_recall": _num(row.get("business_recall")),
        "business_f1": _num(row.get("business_f1")),
        "business_f05": _num(row.get("business_f05")),
    }


def _run_documents(by_doc: list[dict[str, Any]], context: dict[str, Any]) -> str:
    docs = [row.get("scope", "") for row in by_doc if row.get("scope") and row.get("scope") != "[TOTAL]"]
    if docs:
        return "; ".join(dict.fromkeys(docs))
    filters = str(context.get("pdf_filters", "") or "").strip()
    return filters


def _run_summary_row(run_dir: Path) -> dict[str, Any]:
    context = _read_context(run_dir)
    by_doc = _read_csv(run_dir / "metrics_by_document.csv")
    total = next((r for r in by_doc if r.get("scope") == "[TOTAL]"), by_doc[-1] if by_doc else {})
    metrics = _metric_dict(total)
    timestamp_path = run_dir / "run_context.json"
    if not timestamp_path.exists():
        timestamp_path = run_dir / "metrics_by_document.csv"
    timestamp = timestamp_path.stat().st_mtime if timestamp_path.exists() else run_dir.stat().st_mtime
    created_at = datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    run_date, _, run_time = created_at.partition("T")
    document = _run_documents(by_doc, context)
    return {
        "run_dir": str(run_dir),
        "run_name": run_dir.name,
        "created_at": created_at,
        "run_date": run_date,
        "run_time": run_time,
        "document": document,
        "dataset": context.get("dataset", ""),
        "gt_kind": context.get("gt_kind", ""),
        "input_mode": context.get("input_mode", "json"),
        "prompt_text_mode": context.get("prompt_text_mode", ""),
        "force_idp_ocr": context.get("force_idp_ocr", ""),
        "table_context_lines": context.get("table_context_lines", ""),
        "ocr_json_variant": context.get("ocr_json_variant", ""),
        "jobs_effective": context.get("jobs_effective", ""),
        "dataset_items": context.get("dataset_items", ""),
        "pdf_filters": context.get("pdf_filters", ""),
        "company_type": context.get("company_type", ""),
        "role": context.get("role", ""),
        "include_segments": context.get("include_segments", ""),
        "metrics_from_gt": context.get("metrics_from_gt", ""),
        "metric_scope": context.get("metric_scope", ""),
        "score_metric_scope": context.get("score_metric_scope", context.get("metric_scope", "")),
        "real_run_attempts": context.get("real_run_attempts", ""),
        "model_id": context.get("model_id", ""),
        "reference_rows": _int(context.get("reference_rows")),
        "prediction_rows": _int(context.get("prediction_rows")),
        "target_metric_enum_count": _int(context.get("target_metric_enum_count")),
        "extraction_metric_enum_count": _int(context.get("extraction_metric_enum_count")),
        "llm_request_count": _int(context.get("llm_request_count")),
        "tokens_in": _int(context.get("llm_requests_tokens_in")),
        "tokens_out": _int(context.get("llm_requests_tokens_out")),
        "tokens_cached": _int(context.get("llm_requests_tokens_cached")),
        "llm_elapsed_s": _num(context.get("llm_requests_elapsed_s")),
        "cost_usd": _num(context.get("llm_requests_cost_usd")),
        **metrics,
    }


def _collect_run_tables(run_dirs: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []
    docs: list[dict[str, Any]] = []
    enums: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        run = _run_summary_row(run_dir)
        runs.append(run)
        for row in _read_csv(run_dir / "metrics_by_document.csv"):
            if row.get("scope") == "[TOTAL]":
                continue
            docs.append(
                {
                    "run_date": run["run_date"],
                    "run_time": run["run_time"],
                    "run_name": run["run_name"],
                    "run_dir": run["run_dir"],
                    "input_mode": run.get("input_mode", "json"),
                    "prompt_text_mode": run.get("prompt_text_mode", ""),
                    "force_idp_ocr": run.get("force_idp_ocr", ""),
                    "table_context_lines": run.get("table_context_lines", ""),
                    "ocr_json_variant": run.get("ocr_json_variant", ""),
                    "jobs_effective": run.get("jobs_effective", ""),
                    "role": run.get("role", ""),
                    "metric_scope": run.get("metric_scope", ""),
                    "score_metric_scope": run.get("score_metric_scope", ""),
                    "real_run_attempts": run.get("real_run_attempts", ""),
                    "document": row.get("scope") or run.get("document", ""),
                    **row,
                    **_metric_dict(row),
                }
            )
        for row in _read_csv(run_dir / "metrics_by_enum.csv"):
            if row.get("scope") == "[TOTAL]":
                continue
            enums.append(
                {
                    "run_date": run["run_date"],
                    "run_time": run["run_time"],
                    "run_name": run["run_name"],
                    "run_dir": run["run_dir"],
                    "input_mode": run.get("input_mode", "json"),
                    "role": run.get("role", ""),
                    "metric_scope": run.get("metric_scope", ""),
                    "score_metric_scope": run.get("score_metric_scope", ""),
                    "real_run_attempts": run.get("real_run_attempts", ""),
                    "document": run.get("document", ""),
                    **row,
                    **_metric_dict(row),
                }
            )
        for row in _read_csv(run_dir / "row_level_comparison.csv"):
            if row.get("status") not in {"EV", "EFP", "EFN"}:
                continue
            errors.append(
                {
                    "run_date": run["run_date"],
                    "run_time": run["run_time"],
                    "run_name": run["run_name"],
                    "run_dir": run["run_dir"],
                    "input_mode": run.get("input_mode", "json"),
                    "role": run.get("role", ""),
                    "metric_scope": run.get("metric_scope", ""),
                    "score_metric_scope": run.get("score_metric_scope", ""),
                    "real_run_attempts": run.get("real_run_attempts", ""),
                    **row,
                }
            )
    return runs, docs, enums, errors


def _aggregate(rows: list[dict[str, Any]], group_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(field, "") for field in group_fields)
        cur = grouped.setdefault(
            key,
            {field: row.get(field, "") for field in group_fields}
            | {"TP": 0, "EFP": 0, "EFN": 0, "EV": 0, "TN": 0, "runs": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0},
        )
        cur["runs"] += 1
        cur["TP"] += _num(row.get("TP"))
        for field in ("EFP", "EFN", "EV", "TN", "tokens_in", "tokens_out", "tokens_cached"):
            cur[field] += _int(row.get(field))
        cur["cost_usd"] += _num(row.get("cost_usd"))

    out: list[dict[str, Any]] = []
    for row in grouped.values():
        tp, efp, efn, ev = row["TP"], row["EFP"], row["EFN"], row["EV"]
        fp = efp + ev
        fn = efn + ev
        row["precision"] = tp / (tp + fp) if tp + fp else 0.0
        row["recall"] = tp / (tp + fn) if tp + fn else 0.0
        p, r = row["precision"], row["recall"]
        row["f1"] = 2 * p * r / (p + r) if p + r else 0.0
        row["f05"] = 1.25 * p * r / (0.25 * p + r) if p + r else 0.0
        row["accuracy"] = tp / (tp + efp + efn + ev) if tp + efp + efn + ev else 0.0
        row["cost_usd"] = round(row["cost_usd"], 4)
        out.append(row)
    return sorted(out, key=lambda r: tuple(str(r.get(f, "")) for f in group_fields))


def _append_table(ws: openpyxl.worksheet.worksheet.Worksheet, rows: list[dict[str, Any]], headers: list[tuple[str, str]]) -> None:
    ws.append([label for _, label in headers])
    for row in rows:
        values = []
        for key, _ in headers:
            value = row.get(key, "")
            if isinstance(value, float) and key in {"precision", "recall", "f1", "f05", "accuracy", "business_precision", "business_recall", "business_f1", "business_f05"}:
                values.append(round(value * 100, 2))
            else:
                values.append(value)
        ws.append(values)


def _style_sheet(ws: openpyxl.worksheet.worksheet.Worksheet) -> None:
    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in range(1, ws.max_column + 1):
        max_len = len(str(ws.cell(1, col).value or ""))
        for row in range(2, min(ws.max_row, 200) + 1):
            max_len = max(max_len, len(str(ws.cell(row, col).value or "")))
        ws.column_dimensions[get_column_letter(col)].width = min(48, max(10, max_len + 2))


def write_workbook(*, out_xlsx: Path, run_dirs: list[Path], summary_group: str = "run") -> None:
    runs, docs, enums, errors = _collect_run_tables(run_dirs)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводка"
    if summary_group == "role":
        summary_group_fields = (
            "document",
            "dataset",
            "gt_kind",
            "input_mode",
            "prompt_text_mode",
            "force_idp_ocr",
            "table_context_lines",
            "ocr_json_variant",
            "jobs_effective",
            "company_type",
            "role",
            "metric_scope",
            "score_metric_scope",
            "include_segments",
            "metrics_from_gt",
        )
    else:
        summary_group_fields = (
            "run_date",
            "run_time",
            "document",
            "dataset",
            "gt_kind",
            "input_mode",
            "prompt_text_mode",
            "force_idp_ocr",
            "table_context_lines",
            "ocr_json_variant",
            "jobs_effective",
            "company_type",
            "role",
            "metric_scope",
            "score_metric_scope",
            "include_segments",
            "metrics_from_gt",
        )
    summary = _aggregate(
        runs,
        summary_group_fields,
    )
    _append_table(
        ws,
        summary,
        [
            ("run_date", "Run date"),
            ("run_time", "Run time"),
            ("document", "Document"),
            ("dataset", "Dataset"),
            ("gt_kind", "GT kind"),
            ("input_mode", "Input mode"),
            ("prompt_text_mode", "Prompt text mode"),
            ("force_idp_ocr", "Force IDP OCR"),
            ("table_context_lines", "Table context lines"),
            ("ocr_json_variant", "OCR JSON variant"),
            ("jobs_effective", "Jobs"),
            ("company_type", "Company type"),
            ("role", "Role"),
            ("metric_scope", "Extraction metric scope"),
            ("score_metric_scope", "Score metric scope"),
            ("include_segments", "Segments"),
            ("metrics_from_gt", "Metrics from GT"),
            ("runs", "Runs"),
            ("TP", "TP"),
            ("EFP", "EFP"),
            ("EFN", "EFN"),
            ("EV", "EV"),
            ("precision", "Precision %"),
            ("recall", "Recall %"),
            ("f1", "F1 %"),
            ("f05", "F0.5 %"),
            ("accuracy", "Accuracy %"),
            ("tokens_in", "Tokens in"),
            ("tokens_out", "Tokens out"),
            ("tokens_cached", "Cached tokens"),
            ("cost_usd", "Cost, $"),
        ],
    )

    ws = wb.create_sheet("Детали")
    _append_table(
        ws,
        docs,
        [
            ("run_date", "Run date"),
            ("run_time", "Run time"),
            ("document", "Document"),
            ("input_mode", "Input mode"),
            ("prompt_text_mode", "Prompt text mode"),
            ("force_idp_ocr", "Force IDP OCR"),
            ("table_context_lines", "Table context lines"),
            ("ocr_json_variant", "OCR JSON variant"),
            ("jobs_effective", "Jobs"),
            ("role", "Role"),
            ("metric_scope", "Extraction metric scope"),
            ("score_metric_scope", "Score metric scope"),
            ("real_run_attempts", "Real run attempts"),
            ("run_name", "Run name"),
            ("TP", "TP"),
            ("EFP", "EFP"),
            ("EFN", "EFN"),
            ("EV", "EV"),
            ("precision", "Precision %"),
            ("recall", "Recall %"),
            ("f1", "F1 %"),
            ("f05", "F0.5 %"),
            ("accuracy", "Accuracy %"),
            ("run_dir", "Run dir"),
        ],
    )

    ws = wb.create_sheet("По метрикам")
    _append_table(
        ws,
        enums,
        [
            ("run_date", "Run date"),
            ("run_time", "Run time"),
            ("document", "Document"),
            ("input_mode", "Input mode"),
            ("role", "Role"),
            ("metric_scope", "Extraction metric scope"),
            ("score_metric_scope", "Score metric scope"),
            ("real_run_attempts", "Real run attempts"),
            ("run_name", "Run name"),
            ("scope", "Metric enum"),
            ("TP", "TP"),
            ("EFP", "EFP"),
            ("EFN", "EFN"),
            ("EV", "EV"),
            ("precision", "Precision %"),
            ("recall", "Recall %"),
            ("f1", "F1 %"),
            ("f05", "F0.5 %"),
            ("run_dir", "Run dir"),
        ],
    )

    ws = wb.create_sheet("Ошибки")
    _append_table(
        ws,
        errors,
        [
            ("run_date", "Run date"),
            ("run_time", "Run time"),
            ("document", "Document"),
            ("input_mode", "Input mode"),
            ("role", "Role"),
            ("metric_scope", "Extraction metric scope"),
            ("score_metric_scope", "Score metric scope"),
            ("real_run_attempts", "Real run attempts"),
            ("run_name", "Run name"),
            ("dataset", "Dataset"),
            ("enum", "Metric enum"),
            ("period", "Period"),
            ("segment", "Segment"),
            ("status", "Status"),
            ("reasons", "Reason"),
            ("ref_value", "GT value"),
            ("pred_value", "Pred value"),
            ("ref_unit", "GT unit"),
            ("pred_unit", "Pred unit"),
            ("business_score", "Business score"),
            ("run_dir", "Run dir"),
        ],
    )

    ws = wb.create_sheet("Прогоны")
    _append_table(
        ws,
        runs,
        [
            ("run_date", "Run date"),
            ("run_time", "Run time"),
            ("document", "Document"),
            ("run_name", "Run name"),
            ("dataset", "Dataset"),
            ("gt_kind", "GT kind"),
            ("input_mode", "Input mode"),
            ("ocr_json_variant", "OCR JSON variant"),
            ("jobs_effective", "Jobs"),
            ("dataset_items", "Dataset items"),
            ("pdf_filters", "PDF filters"),
            ("company_type", "Company type"),
            ("role", "Role"),
            ("metric_scope", "Extraction metric scope"),
            ("score_metric_scope", "Score metric scope"),
            ("real_run_attempts", "Real run attempts"),
            ("include_segments", "Segments"),
            ("metrics_from_gt", "Metrics from GT"),
            ("target_metric_enum_count", "Target enums"),
            ("extraction_metric_enum_count", "Extraction enums"),
            ("reference_rows", "Reference rows"),
            ("prediction_rows", "Prediction rows"),
            ("TP", "TP"),
            ("EFP", "EFP"),
            ("EFN", "EFN"),
            ("EV", "EV"),
            ("precision", "Precision %"),
            ("recall", "Recall %"),
            ("f1", "F1 %"),
            ("llm_request_count", "LLM requests"),
            ("tokens_in", "Tokens in"),
            ("tokens_out", "Tokens out"),
            ("tokens_cached", "Cached tokens"),
            ("llm_elapsed_s", "LLM elapsed, s"),
            ("cost_usd", "Cost, $"),
            ("model_id", "Model"),
            ("run_dir", "Run dir"),
        ],
    )

    ws = wb.create_sheet("Методика")
    rows = [
        ("Назначение", "Finengine E2E benchmark board for period-aware main metrics and segment rows."),
        ("Источник GT", "tests/test_benchmark/data/golden/<cell>/gt, read by tests/test_benchmark/finengine_e2e_tests/quality.py."),
        ("Источник predictions", "Either fresh copied benchmark runs or collected existing run directories with metrics_by_document.csv / metrics_by_enum.csv / row_level_comparison.csv."),
        ("TP", "Exact/within-tolerance/text match row."),
        ("EFP", "Extra prediction: model returned a row absent from GT."),
        ("EFN", "Missing prediction: row exists in GT but not in model output."),
        ("EV", "Value mismatch: row exists on both sides but value/unit comparison failed."),
        ("Precision", "TP / (TP + EFP + EV)."),
        ("Recall", "TP / (TP + EFN + EV) in the copied E2E scorer output."),
        ("F0.5", "Precision-weighted F-score; useful because false positives are more costly."),
        ("Cost/tokens", "Read from run_context.json llm_request_profile; replay/collect runs have zero or not_used costs."),
        ("No-token rebuild", "Use --mode collect to rebuild this workbook after GT/scorer changes without calling the LLM."),
    ]
    ws.append(["Раздел", "Описание"])
    for row in rows:
        ws.append(row)

    for sheet in wb.worksheets:
        _style_sheet(sheet)

    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_xlsx)


def write_run_workbooks(
    run_dirs: list[Path],
    *,
    filename: str = PER_RUN_XLSX_NAME,
    summary_group: str = "run",
) -> list[Path]:
    written: list[Path] = []
    for run_dir in run_dirs:
        if not (run_dir / "metrics_by_document.csv").exists():
            continue
        out_xlsx = run_dir / filename
        write_workbook(out_xlsx=out_xlsx, run_dirs=[run_dir], summary_group=summary_group)
        written.append(out_xlsx)
    return written


def _selected_cases(suite: str, names: list[str]) -> list[BenchmarkCase]:
    selected = list(CASES)
    if suite != "all":
        selected = [case for case in selected if case.suite == suite]
    if names:
        wanted = set(names)
        selected = [case for case in selected if case.name in wanted]
    if not selected:
        known = ", ".join(case.name for case in CASES)
        raise ValueError(f"No benchmark cases selected. Known cases: {known}")
    return selected


def _parse_pdf_args(raw: list[str]) -> list[str]:
    out: list[str] = []
    for item in raw:
        out.extend(part.strip() for part in item.split(",") if part.strip())
    return out


async def run_cases(args: argparse.Namespace) -> list[Path]:
    if not args.allow_llm and not args.replay_events:
        raise RuntimeError("Running benchmark cases calls the LLM unless --replay-events is supplied. Pass --allow-llm explicitly.")
    out_dirs: list[Path] = []
    for case in _selected_cases(args.suite, args.case):
        repeats = max(1, int(args.repeat or case.repeat or 1))
        pdf_filters = _parse_pdf_args(args.pdf) or list(case.pdf_filters)
        for repeat_idx in range(1, repeats + 1):
            print(f"\n== {case.name} [{repeat_idx}/{repeats}] ==")
            run = await run_finengine_e2e_quality(
                dataset=case.dataset,
                pdf_filters=pdf_filters,
                gt_kind=case.gt_kind,
                gt_xlsx=case.gt_xlsx,
                json_dirs=list(case.json_dirs),
                pdf_dirs=list(case.pdf_dirs),
                input_mode=args.input_mode or case.input_mode,
                prompt_text_mode=args.prompt_text_mode,
                force_idp_ocr=args.force_idp_ocr,
                table_context_lines=args.table_context_lines,
                ocr_json_variant=args.ocr_json_variant,
                jobs=args.jobs,
                dataset_name=case.dataset_name,
                allow_llm=args.allow_llm,
                include_segments=case.include_segments,
                include_consolidated_group=case.include_consolidated_group,
                model_id=args.model_id,
                limit=args.limit or None,
                max_docs=args.max_docs,
                out_dir=args.runs_dir,
                metrics_from_gt=case.metrics_from_gt,
                metric_scope=args.metric_scope if args.metric_scope is not None else case.metric_scope,
                score_metric_scope=(
                    args.score_metric_scope
                    if args.score_metric_scope is not None
                    else case.score_metric_scope
                ),
                real_run_attempts=(
                    args.real_run_attempts
                    if args.real_run_attempts > 0
                    else case.real_run_attempts
                ),
                extractor_matrix=args.extractor_matrix or case.extractor_matrix,
                company_type=case.company_type,
                role=case.role,
                run_name=case.name,
                replay_events=args.replay_events,
                unit_mode=case.unit_mode,
                period_key_mode=case.period_key_mode,
                prediction_scope=case.prediction_scope,
                segment_mode=args.segment_mode or case.segment_mode,
                segment_context_mode=args.segment_context_mode or case.segment_context_mode,
                legacy_replay=args.legacy_replay,
            )
            out_dirs.append(run.run_dir)
            print(run.run_dir)
            print(_format_metrics_line(run.result.macro_by_document))
    return out_dirs


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run/collect copied Finengine E2E benchmark and write Excel board.")
    parser.add_argument("--mode", choices=["collect", "run"], default="collect")
    parser.add_argument("--suite", choices=["main", "full", "segments", "banks_ru", "all"], default="all")
    parser.add_argument("--case", action="append", default=[], help="Specific case name; can be repeated")
    parser.add_argument("--allow-llm", action="store_true", help="Required for real LLM benchmark runs")
    parser.add_argument(
        "--input-mode",
        choices=["json", "pdf"],
        default=None,
        help="Override benchmark cases to use OCR JSON fixtures or locally parsed PDFs",
    )
    parser.add_argument("--pdf", action="append", default=[], help="PDF filename or substring; can be repeated")
    parser.add_argument(
        "--prompt-text-mode",
        choices=sorted(PROMPT_TEXT_MODES),
        default="default",
        help=(
            "Experimental OCR-to-prompt assembly mode for run mode: default is production-like OCR text; "
            "duplicate_tables appends table blocks again; no_tables/text_only omits table blocks; "
            "tables_context appends table blocks with neighbor context."
        ),
    )
    parser.add_argument(
        "--force-idp-ocr",
        action="store_true",
        help="For PDF run mode, skip sibling flat OCR JSON fallback and use/reuse raw IDP OCR cache",
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
        help="For JSON inputs, choose sibling OCR fixtures: old excludes *_new.json, new keeps only *_new.json, both keeps all.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="Max concurrent documents for JSON run mode. PDF run mode is forced to one job.",
    )
    parser.add_argument("--replay-events", type=Path, default=None, help="Replay a prior run dir instead of calling LLM")
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-docs", type=int, default=0)
    parser.add_argument(
        "--metric-scope",
        default=None,
        help="Named main-metric scope or enums:ENUM1,ENUM2 for run mode; segments keep their independent segment scope",
    )
    parser.add_argument(
        "--score-metric-scope",
        default=None,
        help="Optional named main-metric scope or enums:ENUM1,ENUM2 for scoring only; extraction still uses --metric-scope/case scope",
    )
    parser.add_argument(
        "--extractor-matrix",
        action="store_true",
        help="Route run-mode cases through the new per-profile extractor matrix",
    )
    parser.add_argument("--repeat", type=int, default=0, help="Repeat each selected case N times")
    parser.add_argument(
        "--real-run-attempts",
        type=int,
        default=0,
        help="Retry each real document run on transient transport errors up to N attempts",
    )
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--source-runs", type=Path, action="append", default=[], help="Additional run roots to collect")
    parser.add_argument("--out-xlsx", type=Path, default=DEFAULT_OUT_XLSX)
    parser.add_argument(
        "--summary-group",
        choices=["run", "role"],
        default="run",
        help="Aggregate the summary sheet by individual run or by role/scope",
    )
    parser.add_argument(
        "--segment-mode",
        choices=[m.value for m in SegmentMode],
        default=SegmentMode.EXTENDED.value,
        help="Segment extraction/scoring mode for run mode",
    )
    parser.add_argument(
        "--segment-context-mode",
        choices=["full", "focused"],
        default="full",
        help="Segment context mode for run mode: full OCR text or old focused page/anchor selector",
    )
    parser.add_argument(
        "--legacy-replay",
        action="store_true",
        help=(
            "Run cases in legacy cached-replay compatibility mode: keep GT verbatim "
            "and recover accounting signs from source_text. By default cases normalize "
            "GT eagerly and trust period_values.value."
        ),
    )
    parser.add_argument(
        "--skip-run-xlsx",
        action="store_true",
        help=f"Do not write per-run {PER_RUN_XLSX_NAME} files inside run directories",
    )
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    run_dirs: list[Path]
    if args.mode == "run":
        run_dirs = await run_cases(args)
        collect_roots = [args.runs_dir]
    else:
        collect_roots = [args.runs_dir, *args.source_runs]
        run_dirs = []

    collected = _latest_run_dirs(collect_roots)
    if run_dirs:
        selected = {path.resolve() for path in run_dirs}
        collected = [path for path in collected if path.resolve() in selected] + [
            path for path in collected if path.resolve() not in selected
        ]
    write_workbook(out_xlsx=args.out_xlsx, run_dirs=collected, summary_group=args.summary_group)
    per_run = [] if args.skip_run_xlsx else write_run_workbooks(
        collected,
        summary_group=args.summary_group,
    )
    print(f"\nWrote {args.out_xlsx}")
    print(f"Included run dirs: {len(collected)}")
    if per_run:
        print(f"Wrote per-run workbooks: {len(per_run)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
