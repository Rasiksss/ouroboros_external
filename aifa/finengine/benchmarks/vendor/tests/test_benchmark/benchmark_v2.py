"""Finengine DEV benchmark v2 runner and Excel collector.

The v2 benchmark treats per-document rows as the source of truth:

* ``Подробности`` is the append-only ledger: one row is one document in one concrete run.
* ``Прогоны`` and ``Детали`` are rebuilt from the ledger, not from old run artifacts.
* New run artifacts are imported into the existing workbook; deleted artifacts do not delete history.

Examples:
  # Import discovered artifacts into the fixed v2 benchmark ledger, no LLM calls.
  uv run python tests/test_benchmark/benchmark_v2.py --mode collect

  # Run a live golden benchmark and update both the fixed file and an export.
  uv run python tests/test_benchmark/benchmark_v2.py --mode run ^
    --run-name ifrs_bank_ru_context2 --run-type test ^
    --suite golden --golden-basket ifrs_banks_ru --input-mode json ^
    --repeat 3 --allow-llm --out-xlsx tests/test_benchmark/my_run.xlsx
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
from typing import Any, Iterable
from uuid import uuid4

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = ROOT / "tests" / "test_benchmark"
DEFAULT_RUNS_DIR = BENCHMARK_ROOT / "runs"
DEFAULT_BENCHMARK_XLSX = BENCHMARK_ROOT / "Finengine_E2E_Benchmark.xlsx"
GOLDEN_ROOT = BENCHMARK_ROOT / "data" / "golden"
HEARTBEAT_SECONDS = 20
VERBOSITY_LEVELS = {"quiet": 0, "normal": 1, "debug": 2}
_VERBOSITY = "normal"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from test_benchmark.build_benchmark import (  # noqa: E402
    BenchmarkCase,
    _parse_pdf_args,
    _selected_cases,
)
from test_benchmark.finengine_e2e_tests.quality import (  # noqa: E402
    OCR_JSON_VARIANTS,
    PROMPT_TEXT_MODES,
    SegmentMode,
    run_finengine_e2e_quality,
)


@dataclass(frozen=True)
class PromptSuiteVariant:
    key: str
    title: str
    prompt_text_mode: str
    table_context_lines: int


@dataclass(frozen=True)
class RunSlot:
    variant: PromptSuiteVariant
    effective_run_name: str
    case: BenchmarkCase
    pdf_filters: tuple[str, ...]
    repeat_idx: int
    repeats: int
    slot_no: int = 0
    total_slots: int = 0


OCR_ABLATION_VARIANTS: tuple[PromptSuiteVariant, ...] = (
    PromptSuiteVariant("dev_default", "Dev path: full OCR text only", "default", 0),
    PromptSuiteVariant("duplicate_tables", "Full text + duplicated tables", "duplicate_tables", 0),
    PromptSuiteVariant("tables_context2", "Full text + duplicated tables + 2 context lines", "tables_context", 2),
    PromptSuiteVariant("tables_context5", "Full text + duplicated tables + 5 context lines", "tables_context", 5),
    PromptSuiteVariant("tables_context10", "Full text + duplicated tables + 10 context lines", "tables_context", 10),
)

GOLDEN_BASKETS: dict[str, dict[str, Any]] = {
    "us_gaap_nf_en": {
        "dataset_name": "US_GAAP_NF_EN",
        "company_type": "non_financial",
        "include_segments": True,
    },
    "ifrs_nf_ru": {
        "dataset_name": "IFRS_NF_RU",
        "company_type": "non_financial",
        "include_segments": True,
    },
    "ifrs_nf_en": {
        "dataset_name": "IFRS_NF_EN",
        "company_type": "non_financial",
        "include_segments": True,
    },
    "us_gaap_banks_en": {
        "dataset_name": "US_GAAP_BANKS_EN",
        "company_type": "bank",
        "include_segments": False,
    },
    "ifrs_banks_en": {
        "dataset_name": "IFRS_BANKS_EN",
        "company_type": "bank",
        "include_segments": False,
    },
    "ifrs_banks_ru": {
        "dataset_name": "IFRS_BANKS_RU",
        "company_type": "bank",
        "include_segments": False,
        "include_consolidated_group": True,
    },
}

GOLDEN_CASES: dict[str, dict[str, tuple[str, ...]]] = {
    "model_eval_12": {
        "us_gaap_nf_en": (
            "amazon-6M--2025-06-30",
            "intel-9M--2025-09-27",
        ),
        "ifrs_nf_ru": (
            "severstal-Q1-2025",
            "cherkizovo-2021",
        ),
        "ifrs_nf_en": (
            "japan-airlines-6M--30.09.2025",
            "samsung-2024",
        ),
        "us_gaap_banks_en": (
            "capital-one-financial-Q3-2025",
            "morgan-stanley-Q2-2025",
        ),
        "ifrs_banks_en": (
            "bnp-paribas-Q2-2025",
            "hsbc-holdings-Q2-2025",
        ),
        "ifrs_banks_ru": (
            "metallinvestbank-2024",
            "bank-sankt-peterburg-Q2-2025",
        ),
    },
}


METRIC_FIELDS = [
    "precision",
    "recall",
    "f1",
    "accuracy",
    "tp_accuracy",
    "business_tp_sum",
    "tp_like_slots",
    "TP",
    "EFP",
    "EFN",
    "EV",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "duration_sec",
]
RUN_METRIC_FIELDS = [
    "precision",
    "recall",
    "f1",
    "accuracy",
    "tp_accuracy",
    "cost_total_real_llm",
    "cost_avg_real_llm_run",
    "input_tokens",
    "output_tokens",
    "duration_min",
    "duration_min_avg",
]
DETAIL_GROUPS = [
    ("overall", "overall_main_segments"),
    ("main", "main"),
    ("segments", "segments"),
    ("consolidated_group", "consolidated_group"),
    ("top_borrowers", "top_borrowers"),
]
RUN_GROUPS = [
    ("overall", "overall_main_segments"),
    ("main", "main"),
    ("segments", "segments"),
]

VALID_LLM_STATUSES = {"ok", "replay"}
REPLAY_SOURCE_STATUSES = {"failed", "empty", "token_limit", "invalid"}
INVALID_METRIC_VALUE = "invalid"
TELEMETRY_METRIC_FIELDS = {"cost_usd", "input_tokens", "output_tokens", "duration_sec"}


class BenchmarkHistoryError(RuntimeError):
    """Raised when importing rows would make benchmark history ambiguous."""


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _log(message: str, *, level: str = "normal") -> None:
    if VERBOSITY_LEVELS.get(level, 1) > VERBOSITY_LEVELS.get(_VERBOSITY, 1):
        return
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, rest = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {rest:02d}s"
    if minutes:
        return f"{minutes}m {rest:02d}s"
    return f"{rest}s"


DOC_COLUMNS = ("document", "doc_name", "pdf", "pdf_file", "file", "filename", "filepath", "Filepath", "source_file")


def _read_gt_documents(path: Path | None) -> tuple[str, ...]:
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return ()
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = csv.DictReader(text.splitlines(), dialect=dialect)
    docs: list[str] = []
    seen: set[str] = set()
    for row in rows:
        doc = ""
        for column in DOC_COLUMNS:
            doc = str(row.get(column) or "").strip()
            if doc:
                break
        key = doc.lower()
        if doc and key not in seen:
            docs.append(doc)
            seen.add(key)
    return tuple(docs)


def _match_pdf_filters(documents: tuple[str, ...], raw_filters: list[str]) -> tuple[str, ...]:
    if not raw_filters:
        return ()
    matched: list[str] = []
    for document in documents:
        document_key = document.lower()
        if any(
            raw.lower() == document_key
            or raw.lower() in document_key
            or document_key in raw.lower()
            for raw in raw_filters
        ):
            matched.append(document)
    return tuple(matched)


def _prompt_suite_variants(args: argparse.Namespace) -> tuple[PromptSuiteVariant, ...]:
    if args.prompt_suite == "single":
        return (
            PromptSuiteVariant(
                "single",
                "Single prompt assembly",
                args.prompt_text_mode,
                args.table_context_lines,
            ),
        )
    if args.prompt_suite == "ocr_ablation":
        return OCR_ABLATION_VARIANTS
    raise ValueError(f"Unknown prompt suite: {args.prompt_suite}")


def _effective_run_name(base_name: str, variant: PromptSuiteVariant, prompt_suite: str) -> str:
    if prompt_suite == "single":
        return base_name
    return f"{base_name}_{variant.key}"


def _requested_run_names(args: argparse.Namespace) -> set[str]:
    if not args.run_name:
        return set()
    return {
        _effective_run_name(args.run_name, variant, args.prompt_suite)
        for variant in _prompt_suite_variants(args)
    }


def _existing_workbook_run_names(xlsx_path: Path) -> set[str]:
    if not xlsx_path.exists():
        return set()
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    if "Подробности" not in wb.sheetnames:
        return set()
    ws = wb["Подробности"]
    run_names: set[str] = set()
    for (value,) in ws.iter_rows(min_row=3, min_col=4, max_col=4, values_only=True):
        text = str(value or "").strip()
        if text:
            run_names.add(text)
    return run_names


def _preflight_existing_run_names(args: argparse.Namespace) -> None:
    if args.mode != "run" or args.append_to_existing_run:
        return
    requested = _requested_run_names(args)
    if not requested:
        return
    workbooks = [DEFAULT_BENCHMARK_XLSX]
    if args.out_xlsx and args.out_xlsx.exists() and args.out_xlsx != DEFAULT_BENCHMARK_XLSX:
        workbooks.append(args.out_xlsx)
    conflicts: dict[Path, set[str]] = {}
    for workbook in workbooks:
        existing = _existing_workbook_run_names(workbook)
        overlap = requested & existing
        if overlap:
            conflicts[workbook] = overlap
    if not conflicts:
        return
    details = "; ".join(
        f"{path}: {', '.join(sorted(names))}"
        for path, names in conflicts.items()
    )
    raise BenchmarkHistoryError(
        "Benchmark workbook already contains requested run_name(s) before the LLM run starts: "
        f"{details}. Use --append/--append-to-existing-run for an intentional rerun/replay "
        "with the same name."
    )


def _golden_case_stems(
    basket_dir: Path,
    *,
    raw_filters: list[str],
    max_docs: int = 0,
) -> list[str]:
    gt_dir = basket_dir / "gt"
    stems = sorted(path.stem for path in gt_dir.glob("*.csv") if path.name != "gt.csv")
    if raw_filters:
        selected: list[str] = []
        for stem in stems:
            candidates = {stem.lower(), f"{stem}.pdf".lower(), f"{stem}.json".lower()}
            if any(
                raw.lower() in candidate or candidate in raw.lower()
                for raw in raw_filters
                for candidate in candidates
            ):
                selected.append(stem)
        stems = selected
    if max_docs > 0:
        stems = stems[:max_docs]
    return stems


def _golden_cases(args: argparse.Namespace) -> list[BenchmarkCase]:
    baskets = args.golden_basket or []
    selected_by_case: dict[str, set[str]] = {}
    for case_name in args.golden_case:
        if case_name not in GOLDEN_CASES:
            known = ", ".join(sorted(GOLDEN_CASES))
            raise ValueError(f"Unknown golden case: {case_name}. Known cases: {known}")
        for basket, stems in GOLDEN_CASES[case_name].items():
            selected_by_case.setdefault(basket, set()).update(stems)
            if basket not in baskets:
                baskets.append(basket)
    if args.suite == "golden" and not baskets:
        baskets = sorted(GOLDEN_BASKETS)
    raw_filters = _parse_pdf_args(args.pdf)
    cases: list[BenchmarkCase] = []
    for basket in baskets:
        if basket not in GOLDEN_BASKETS:
            known = ", ".join(sorted(GOLDEN_BASKETS))
            raise ValueError(f"Unknown golden basket: {basket}. Known baskets: {known}")
        basket_dir = args.golden_root / basket
        gt_dir = basket_dir / "gt"
        json_dir = basket_dir / "json"
        pdf_dir = basket_dir / "pdf"
        if not gt_dir.exists():
            raise ValueError(f"Golden basket has no gt directory: {gt_dir}")
        meta = GOLDEN_BASKETS[basket]
        stems = _golden_case_stems(basket_dir, raw_filters=raw_filters, max_docs=args.max_docs)
        if basket in selected_by_case:
            expected = selected_by_case.get(basket, set())
            stems = [stem for stem in stems if stem in expected]
            missing = sorted(expected - set(stems))
            if missing and not raw_filters:
                _log(f"Warning: golden case references missing files in {basket}: {', '.join(missing)}")
        for stem in stems:
            gt_csv = gt_dir / f"{stem}.csv"
            json_path = json_dir / f"{stem}.json"
            pdf_path = pdf_dir / f"{stem}.pdf"
            missing = [str(path) for path in (gt_csv, json_path, pdf_path) if not path.exists()]
            if missing:
                _log(f"Warning: skipping golden case {basket}/{stem}; missing: {', '.join(missing)}")
                continue
            cases.append(
                BenchmarkCase(
                    name=f"golden_{basket}_{stem}",
                    suite="golden",
                    dataset="custom",
                    dataset_name=meta["dataset_name"],
                    gt_kind="full",
                    gt_xlsx=gt_csv,
                    json_dirs=(json_dir,),
                    pdf_dirs=(pdf_dir,),
                    pdf_filters=(f"{stem}.pdf",),
                    company_type=meta["company_type"],
                    include_segments=bool(meta.get("include_segments")),
                    include_consolidated_group=bool(meta.get("include_consolidated_group")),
                    extractor_matrix=True,
                    role=args.role or "other",
                    input_mode="json",
                    metric_scope=args.metric_scope or "full",
                    score_metric_scope=args.score_metric_scope,
                    segment_mode=args.segment_mode,
                    segment_context_mode=args.segment_context_mode,
                    period_key_mode="STRICT",
                    # Golden GT is complete for main metrics: an in-scope
                    # prediction absent from GT is a hallucination -> EFP.
                    # Segment predictions stay reference-filtered.
                    prediction_scope="MAIN_STRICT",
                    real_run_attempts=args.real_run_attempts or 1,
                )
            )
    return cases


def _selected_cases_v2(args: argparse.Namespace) -> list[BenchmarkCase]:
    if args.suite == "golden" or args.golden_basket:
        cases = _golden_cases(args)
        if not cases:
            raise ValueError("No golden benchmark cases selected.")
        return cases
    return _selected_cases(args.suite, args.case)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in headers})


def _read_context(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run_context.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", ".")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _int(value: Any, default: int = 0) -> int:
    return int(round(_num(value, float(default))))


def _format_quality_metric(metric: Any) -> str:
    values = metric.values
    counts = metric.counts
    return (
        f"P={values.precision:.2%} R={values.recall:.2%} F1={values.f1:.2%} "
        f"TP={counts.TP:g} EFP={counts.EFP:g} EFN={counts.EFN:g} EV={counts.EV:g}"
    )


def _format_usage(context: dict[str, Any]) -> str:
    cost = _num(context.get("llm_requests_cost_usd"))
    tokens_in = _int(context.get("llm_requests_tokens_in"))
    tokens_out = _int(context.get("llm_requests_tokens_out"))
    elapsed = _num(context.get("llm_requests_elapsed_s"))
    calls = _int(context.get("llm_request_count"))
    parts = [f"llm_calls={calls}"]
    if cost:
        parts.append(f"cost=${cost:.4f}")
    if tokens_in or tokens_out:
        parts.append(f"tokens={tokens_in}/{tokens_out}")
    if elapsed:
        parts.append(f"llm_elapsed={_format_elapsed(elapsed)}")
    return ", ".join(parts)


async def _heartbeat(label: str, start: float, progress_state: dict[str, str] | None = None) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            elapsed = asyncio.get_running_loop().time() - start
            stage = ""
            if progress_state:
                current = progress_state.get("message", "").strip()
                if current:
                    stage = f"; stage={current}"
            _log(f"{label}: still running after {_format_elapsed(elapsed)}{stage}", level="debug")
    except asyncio.CancelledError:
        return


def _blank_if_none(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if p + r else 0.0


def _metric_values(
    *,
    TP: float = 0.0,
    EFP: float = 0.0,
    EFN: float = 0.0,
    EV: float = 0.0,
    business_tp_sum: float = 0.0,
    tp_like_slots: float = 0.0,
    cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    precision = _safe_div(TP, TP + EFP + EV)
    recall = _safe_div(TP, TP + EFN + EV)
    all_slots = TP + EFP + EFN + EV
    return {
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "accuracy": _safe_div(TP, all_slots),
        "tp_accuracy": _safe_div(business_tp_sum, tp_like_slots),
        "business_tp_sum": business_tp_sum,
        "tp_like_slots": tp_like_slots,
        "TP": TP,
        "EFP": EFP,
        "EFN": EFN,
        "EV": EV,
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_sec": duration_sec,
    }


def _metrics_from_flat(row: dict[str, Any]) -> dict[str, Any]:
    return _metric_values(
        TP=_num(row.get("TP")),
        EFP=_num(row.get("EFP")),
        EFN=_num(row.get("EFN")),
        EV=_num(row.get("EV")),
        business_tp_sum=_num(row.get("business_tp_sum")),
        tp_like_slots=_num(row.get("tp_like_slots")),
    )


def _metrics_from_comparisons(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    TP = EFP = EFN = EV = business_tp_sum = tp_like_slots = 0.0
    for row in rows:
        status = str(row.get("status") or "").strip()
        business_score = _num(row.get("business_score"))
        if status == "TP":
            TP += business_score or 1.0
            business_tp_sum += business_score
            tp_like_slots += 1
        elif status == "EFP":
            EFP += 1
        elif status == "EFN":
            EFN += 1
        elif status == "EV":
            EV += 1
            business_tp_sum += business_score
            tp_like_slots += 1
    return _metric_values(
        TP=TP,
        EFP=EFP,
        EFN=EFN,
        EV=EV,
        business_tp_sum=business_tp_sum,
        tp_like_slots=tp_like_slots,
    )


def _parse_mapping(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    text = str(raw).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    try:
        parsed = ast.literal_eval(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not raw:
        return []
    text = str(raw).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except Exception:
            parsed = []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _git_value(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _git_metadata() -> dict[str, str]:
    branch = _git_value(["rev-parse", "--abbrev-ref", "HEAD"])
    commit_sha = _git_value(["rev-parse", "HEAD"])
    log = _git_value(["log", "--max-count=60", "--pretty=%s"])
    pr_number = ""
    for line in log.splitlines():
        match = re.search(r"#(\d+)", line)
        if match and ("Запрос на слияние" in line or "pull" in line.lower() or "merge" in line.lower()):
            pr_number = match.group(1)
            break
    return {"branch": branch, "commit_sha": commit_sha, "pr_number": pr_number}


def _run_timestamp(run_dir: Path) -> datetime:
    candidates = [run_dir.stat().st_mtime]
    for name in ("run_context.json", "metrics_by_document.csv"):
        path = run_dir / name
        if path.exists():
            candidates.append(path.stat().st_mtime)
    return datetime.fromtimestamp(max(candidates))


def _find_run_dirs(roots: list[Path], since: str | None = None) -> list[Path]:
    cutoff: float | None = None
    if since:
        cutoff = datetime.fromisoformat(since).timestamp()
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("metrics_by_document.csv"):
            run_dir = path.parent
            if cutoff is not None and _run_timestamp(run_dir).timestamp() < cutoff:
                continue
            out.append(run_dir)
    return sorted(dict.fromkeys(out), key=lambda item: _run_timestamp(item))


def _profile_to_standard_language(profile_id: str | None) -> tuple[str, str]:
    if not profile_id:
        return "", ""
    text = profile_id.lower()
    standard = "US GAAP" if "us_gaap" in text else "IFRS" if "ifrs" in text else ""
    language = "ru" if text.endswith("_ru") or "_ru" in text else "en" if text.endswith("_en") or "_en" in text else ""
    return standard, language


def _std_lang_from_run(run_dir: Path, context: dict[str, Any], records: list[dict[str, str]]) -> tuple[str, str, str, str]:
    for record in records:
        diag = _parse_mapping(record.get("_diag") or record.get("diag"))
        # Replay records carry profile_detected at the top level (captured from
        # the events file), not under diag.
        profile = (diag.get("profile_detected") if isinstance(diag, dict) else None) or _parse_mapping(
            record.get("profile_detected")
        )
        if isinstance(profile, dict) and profile:
            standard = str(profile.get("standard") or "").upper().replace("US_GAAP", "US GAAP")
            if standard == "IFRS":
                standard = "IFRS"
            language = str(profile.get("language") or "")
            company_type = str(profile.get("company_type") or record.get("company_type") or context.get("company_type") or "")
            return standard or "unknown", language or "unknown", company_type, "classifier"
        routed_to = str(diag.get("routed_to") or "")
        standard, language = _profile_to_standard_language(routed_to)
        if standard or language:
            return standard or "unknown", language or "unknown", str(record.get("company_type") or context.get("company_type") or ""), "classifier"
    text = " ".join(str(part) for part in [run_dir.as_posix(), context.get("dataset_items"), context.get("dataset"), context.get("company_type")] if part).lower()
    standard = "US GAAP" if "usgaap" in text or "us_gaap" in text else "IFRS" if "ifrs" in text or "banks_ru" in text else "unknown"
    language = "ru" if "banks_ru" in text or re.search(r"[а-яё]", text) else "en" if "usgaap" in text else "unknown"
    return standard, language, str(context.get("company_type") or ""), "legacy_infer"


def _record_key(record: dict[str, str]) -> str:
    return str(record.get("pdf_name") or record.get("input") or record.get("document") or "").strip()


def _record_profile(records: list[dict[str, str]], document: str) -> dict[str, Any]:
    candidates = [record for record in records if _record_key(record) == document]
    if not candidates:
        candidates = [record for record in records if Path(_record_key(record)).name == Path(document).name]
    if not candidates:
        return {
            "llm_usage": "replay",
            "llm_status": "replay",
            "classifier_status": "unknown",
            "cost_usd": None,
            "input_tokens": None,
            "output_tokens": None,
            "duration_sec": None,
            "llm_requests": None,
        }
    cost = 0.0
    tokens_in = 0
    tokens_out = 0
    elapsed = 0.0
    request_count = 0
    errors: list[str] = []
    classifier_status = "unknown"
    live = False
    for record in candidates:
        diag = _parse_mapping(record.get("_diag") or record.get("diag"))
        requests = _parse_list(record.get("llm_requests")) or _parse_list(diag.get("llm_requests"))
        if requests:
            live = True
        for request in requests:
            request_count += 1
            cost += _num(request.get("cost_usd"))
            tokens_in += _int(request.get("tokens_in"))
            tokens_out += _int(request.get("tokens_out"))
            elapsed += _num(request.get("elapsed_s"))
        if not requests and diag:
            diag_cost = _num(diag.get("cost_usd"))
            diag_in = _int(diag.get("tokens_in"))
            diag_out = _int(diag.get("tokens_out"))
            diag_calls = _int(diag.get("llm_calls"))
            if diag_cost or diag_in or diag_out or diag_calls:
                live = True
                request_count += diag_calls or 1
                cost += diag_cost
                tokens_in += diag_in
                tokens_out += diag_out
                elapsed += _num(diag.get("llm_stream_s") or diag.get("elapsed_s"))
        if not requests and not (diag and (_num(diag.get("cost_usd")) or _int(diag.get("tokens_in")))):
            # Replay: the original run's usage sits at the record top level
            # (merged from the events `done` event), not under diag. Surface it
            # so cost/tokens aren't zero — but keep the row labelled as replay.
            cost += _num(record.get("cost_usd"))
            tokens_in += _int(record.get("tokens_in"))
            tokens_out += _int(record.get("tokens_out"))
        if record.get("elapsed_s"):
            elapsed = max(elapsed, _num(record.get("elapsed_s")))
        if record.get("error"):
            errors.append(str(record.get("error")))
        if diag.get("profile_detected") or diag.get("routed_to") or record.get("profile_detected"):
            classifier_status = "ran_ok"
    if errors:
        joined = " ".join(errors).lower()
        if "limit" in joined or "token" in joined or "лимит" in joined or "ключ" in joined:
            llm_status = "token_limit"
        elif "не вернула ответ" in joined or "empty" in joined or "пуст" in joined:
            llm_status = "empty"
        else:
            llm_status = "failed"
    else:
        llm_status = "ok" if live else "replay"
    # Replay rows have real usage (from the original run's events) but no fresh
    # LLM call, so show cost/tokens whenever we have them, while llm_requests
    # (fresh calls) stays 0 for a replay.
    has_usage = live or bool(cost or tokens_in or tokens_out)
    return {
        "llm_usage": "live_llm" if live else "replay",
        "llm_status": llm_status,
        "classifier_status": classifier_status,
        "cost_usd": round(cost, 4) if has_usage else 0.0,
        "input_tokens": tokens_in if has_usage else 0,
        "output_tokens": tokens_out if has_usage else 0,
        "duration_sec": round(elapsed, 1) if elapsed else None,
        "llm_requests": request_count if live else 0,
    }


def _legacy_run_type(run_dir: Path, context: dict[str, Any], doc_count: int) -> str:
    text = f"{run_dir.as_posix()} {context.get('dataset_items','')} {context.get('prompt_text_mode','')}".lower()
    if any(token in text for token in ["ablation", "worst5", "narrow", "mib_die_vs_other", "experiment"]):
        return "test"
    if "golden_" in text or "/golden" in text:
        return "control"
    if doc_count >= 7 or any(token in text for token in ["22docs", "16docs", "quality_selected", "benchmark_ifrs_banks_ru"]):
        return "control"
    return "test"


def _legacy_run_name(run_dir: Path, context: dict[str, Any]) -> str:
    meta = context.get("benchmark_v2")
    if isinstance(meta, dict) and meta.get("run_name"):
        return str(meta["run_name"])
    try:
        rel = run_dir.relative_to(DEFAULT_RUNS_DIR)
        if len(rel.parts) > 1:
            return rel.parts[0]
    except ValueError:
        pass
    return run_dir.name


def _legacy_repeat_no(run_dir: Path, context: dict[str, Any]) -> int:
    meta = context.get("benchmark_v2")
    if isinstance(meta, dict) and meta.get("repeat_no"):
        return _int(meta.get("repeat_no"), 1)
    match = re.search(r"_(\d+)$", run_dir.name)
    if match:
        return int(match.group(1))
    return 1


@dataclass
class Col:
    key: str
    label: str
    width: float = 14.0
    number_format: str | None = None
    group: str = ""
    hidden_group: bool = False


def _base_detail_columns(full: bool) -> list[Col]:
    cols = [
        Col("run_type", "тип", 12),
        Col("llm_status", "статус обращения к ЛЛМ", 18),
        Col("pr_number", "номер PR", 10),
        Col("run_name", "название прогона", 34),
        Col("repeat_no", "номер рана", 10, "0"),
        Col("document", "документ", 32),
        Col("standard", "стандарт", 12),
        Col("language", "язык", 8),
        Col("company_type", "тип компании", 14),
        Col("role", "роль", 12),
        Col("run_date", "дата", 12),
        Col("run_time", "время", 10),
        Col("model", "model", 22),
    ]
    for key, label in DETAIL_GROUPS:
        hidden = key in {"segments", "consolidated_group", "top_borrowers"}
        for field in METRIC_FIELDS:
            fmt = "0.00%" if field in {"precision", "recall", "f1", "accuracy", "tp_accuracy"} else "0.00"
            cols.append(Col(f"{key}_{field}", field, 12, fmt, group=label, hidden_group=hidden))
    cols.append(Col("llm_requests", "LLM requests", 13, "0"))
    if full:
        cols.extend(
            [
                Col("source_group_run_path", "Группа/ран", 42),
                Col("input_mode", "Input mode", 12),
                Col("experiment", "Эксперимент", 32),
                Col("prompt_suite", "Prompt suite", 16),
                Col("prompt_variant", "Prompt variant", 20),
                Col("prompt_text_mode", "Prompt mode", 18),
                Col("table_context_lines", "Table context", 14, "0"),
                Col("ocr_json_variant", "OCR JSON", 12),
                Col("metric_scope", "Metric scope", 18),
                Col("score_scope", "Score scope", 18),
                Col("segments_enabled", "Segments", 10),
                Col("run_dir", "Run dir", 54),
                Col("std_lang_source", "Источник std/lang", 18),
                Col("aggregation_selected", "aggregation_selected", 18),
                Col("run_id", "run_id", 36),
                Col("document_run_id", "document_run_id", 36),
                Col("commit_sha", "commit_sha", 42),
                Col("branch", "branch", 18),
                Col("source_kind", "source_kind", 16),
                Col("source_run_dirs", "source_run_dirs", 54),
                Col("comment", "Комментарий", 36),
            ]
        )
        cols.insert(-1, Col("classifier_status", "статус классификатора", 18))
    else:
        cols.append(Col("classifier_status", "статус классификатора", 18))
    return cols


def _run_columns() -> list[Col]:
    cols = [
        Col("run_type", "тип", 12),
        Col("pr_number", "номер PR", 10),
        Col("run_name", "название прогона", 34),
        Col("run_date", "дата", 12),
        Col("run_time", "время", 10),
        Col("model", "model", 22),
        Col("document_count", "количество документов", 16, "0"),
        Col("run_count", "количество проходов", 16, "0"),
    ]
    for key, label in RUN_GROUPS:
        hidden = key in {"main", "segments"}
        for field in RUN_METRIC_FIELDS:
            fmt = "0.00%" if field in {"precision", "recall", "f1", "accuracy", "tp_accuracy"} else "0.00"
            cols.append(Col(f"{key}_{field}", field, 13, fmt, group=label, hidden_group=hidden))
    cols.extend(
        [
            Col("f1_mean_deviation", "F1 mean deviation", 16, "0.00%"),
            Col("f1_std", "F1 std", 12, "0.00%"),
            Col("f1_range", "F1 range", 12, "0.00%"),
            Col("documents", "документы", 54),
            Col("source_run_dirs", "source_run_dirs", 54),
        ]
    )
    return cols


def _flatten_group_metrics(row: dict[str, Any], group_key: str, metrics: dict[str, Any], prefix: str | None = None) -> None:
    prefix = prefix or group_key
    for field in METRIC_FIELDS:
        row[f"{prefix}_{field}"] = metrics.get(field)


def _comparison_groups(run_dir: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    by_doc: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in _read_csv(run_dir / "row_level_comparison.csv"):
        doc = str(row.get("document") or "")
        segment = str(row.get("segment") or "")
        doc_groups = by_doc.setdefault(doc, {"overall": [], "main": [], "segments": [], "consolidated_group": [], "top_borrowers": []})
        doc_groups["overall"].append(row)
        if segment:
            doc_groups["segments"].append(row)
        else:
            doc_groups["main"].append(row)
    return by_doc


def collect_detail_rows(run_dirs: list[Path], *, source_kind: str = "legacy_import") -> list[dict[str, Any]]:
    git_meta = _git_metadata()
    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        context = _read_context(run_dir)
        records = _read_csv(run_dir / "run_records.csv")
        by_doc = [row for row in _read_csv(run_dir / "metrics_by_document.csv") if row.get("scope") != "[TOTAL]"]
        if not by_doc:
            continue
        timestamp = _run_timestamp(run_dir)
        run_name = _legacy_run_name(run_dir, context)
        repeat_no = _legacy_repeat_no(run_dir, context)
        meta = context.get("benchmark_v2") if isinstance(context.get("benchmark_v2"), dict) else {}
        run_type = str(meta.get("run_type") or _legacy_run_type(run_dir, context, len(by_doc)))
        run_id = str(meta.get("run_id") or uuid4())
        standard, language, company_type, std_lang_source = _std_lang_from_run(run_dir, context, records)
        comparison_groups = _comparison_groups(run_dir)
        run_dir_rel = str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir)
        for doc_row in by_doc:
            document = str(doc_row.get("scope") or "")
            record_profile = _record_profile(records, document)
            group_rows = comparison_groups.get(document, {})
            detail: dict[str, Any] = {
                "run_type": run_type,
                "llm_usage": str(meta.get("llm_usage") or record_profile["llm_usage"]),
                "llm_status": str(record_profile["llm_status"]),
                "classifier_status": str(record_profile["classifier_status"]),
                "pr_number": str(meta.get("pr_number") or git_meta.get("pr_number") or ""),
                "run_name": run_name,
                "repeat_no": repeat_no,
                "document": document,
                "standard": standard,
                "language": language,
                "company_type": company_type or str(context.get("company_type") or ""),
                "role": str(context.get("role") or ""),
                "run_date": timestamp.strftime("%Y-%m-%d"),
                "run_time": timestamp.strftime("%H:%M:%S"),
                "model": str(context.get("model_id") or ""),
                "llm_requests": record_profile["llm_requests"],
                "source_group_run_path": run_dir_rel,
                "input_mode": str(context.get("input_mode") or ""),
                "experiment": str(meta.get("prompt_variant_title") or meta.get("experiment") or ""),
                "prompt_suite": str(meta.get("prompt_suite") or ""),
                "prompt_variant": str(meta.get("prompt_variant") or ""),
                "prompt_text_mode": str(context.get("prompt_text_mode") or meta.get("prompt_text_mode") or ""),
                "table_context_lines": _int(context.get("table_context_lines") or meta.get("table_context_lines"), 0),
                "ocr_json_variant": str(context.get("ocr_json_variant") or meta.get("ocr_json_variant") or ""),
                "metric_scope": str(context.get("metric_scope") or "full"),
                "score_scope": str(context.get("score_metric_scope") or context.get("metric_scope") or "full"),
                "segments_enabled": str(context.get("include_segments") or ""),
                "run_dir": run_dir_rel,
                "std_lang_source": std_lang_source,
                "aggregation_selected": False,
                "run_id": run_id,
                "document_run_id": f"{run_id}:{document}",
                "commit_sha": str(meta.get("commit_sha") or git_meta.get("commit_sha") or ""),
                "branch": str(meta.get("branch") or git_meta.get("branch") or ""),
                "source_kind": str(meta.get("source_kind") or source_kind),
                "source_run_dirs": run_dir_rel,
                "comment": "",
            }
            overall_metrics = _metrics_from_comparisons(group_rows.get("overall", [])) if group_rows else _metrics_from_flat(doc_row)
            if not group_rows:
                main_metrics = overall_metrics
                segment_metrics = _metric_values()
            else:
                main_metrics = _metrics_from_comparisons(group_rows.get("main", []))
                segment_metrics = _metrics_from_comparisons(group_rows.get("segments", []))
            for metrics in (overall_metrics, main_metrics, segment_metrics):
                metrics["cost_usd"] = record_profile["cost_usd"]
                metrics["input_tokens"] = record_profile["input_tokens"]
                metrics["output_tokens"] = record_profile["output_tokens"]
                metrics["duration_sec"] = record_profile["duration_sec"]
            _flatten_group_metrics(detail, "overall", overall_metrics)
            _flatten_group_metrics(detail, "main", main_metrics)
            _flatten_group_metrics(detail, "segments", segment_metrics)
            _flatten_group_metrics(detail, "consolidated_group", _metric_values())
            _flatten_group_metrics(detail, "top_borrowers", _metric_values())
            rows.append(detail)
    _apply_detail_status_rules(rows)
    return rows


def _row_datetime(row: dict[str, Any]) -> str:
    return f"{row.get('run_date') or ''} {row.get('run_time') or ''} {row.get('run_id') or ''}"


def _has_value(value: Any) -> bool:
    return value not in {None, "", INVALID_METRIC_VALUE}


def _copy_replay_runtime_from_source(replay_row: dict[str, Any], source_row: dict[str, Any]) -> None:
    for group_key, _ in DETAIL_GROUPS:
        for field in ("cost_usd", "duration_sec"):
            source_value = source_row.get(f"{group_key}_{field}")
            if _has_value(source_value):
                replay_row[f"{group_key}_{field}"] = source_value


def _mark_metric_fields_invalid(row: dict[str, Any]) -> None:
    for group_key, _ in DETAIL_GROUPS:
        for field in METRIC_FIELDS:
            if field in TELEMETRY_METRIC_FIELDS:
                continue
            row[f"{group_key}_{field}"] = INVALID_METRIC_VALUE


def _apply_detail_status_rules(rows: list[dict[str, Any]]) -> None:
    by_run_document: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("run_name") or ""), str(row.get("document") or ""))
        by_run_document.setdefault(key, []).append(row)

    for pair_rows in by_run_document.values():
        ordered = sorted(pair_rows, key=_row_datetime)
        for idx, row in enumerate(ordered):
            row_status = str(row.get("llm_status") or "")
            if row_status not in VALID_LLM_STATUSES:
                continue
            sources = [
                candidate
                for candidate in ordered[:idx]
                if str(candidate.get("llm_status") or "") in REPLAY_SOURCE_STATUSES
            ]
            if not sources:
                continue
            source = sources[-1]
            if row_status == "replay":
                _copy_replay_runtime_from_source(row, source)
            for candidate in sources:
                candidate["llm_status"] = "invalid"

    for row in rows:
        if str(row.get("llm_status") or "") not in VALID_LLM_STATUSES:
            _mark_metric_fields_invalid(row)


def _is_success(row: dict[str, Any]) -> bool:
    return str(row.get("llm_status") or "") in VALID_LLM_STATUSES


def select_aggregation_rows(detail_rows: list[dict[str, Any]]) -> set[str]:
    selected_ids: set[str] = set()
    by_run_name: dict[str, list[dict[str, Any]]] = {}
    for row in detail_rows:
        by_run_name.setdefault(str(row.get("run_name") or ""), []).append(row)
    for run_rows in by_run_name.values():
        repeat_count = max(1, max(_int(row.get("repeat_no"), 1) for row in run_rows))
        by_document: dict[str, list[dict[str, Any]]] = {}
        for row in run_rows:
            by_document.setdefault(str(row.get("document") or ""), []).append(row)
        for rows in by_document.values():
            rows_sorted = sorted(rows, key=_row_datetime, reverse=True)
            success = [row for row in rows_sorted if _is_success(row)]
            chosen = success[:repeat_count]
            for row in chosen:
                selected_ids.add(str(row.get("document_run_id")))
    for row in detail_rows:
        row["aggregation_selected"] = str(row.get("document_run_id")) in selected_ids
    return selected_ids


def _aggregate_group(rows: list[dict[str, Any]], group_key: str) -> dict[str, Any]:
    TP = sum(_num(row.get(f"{group_key}_TP")) for row in rows)
    EFP = sum(_num(row.get(f"{group_key}_EFP")) for row in rows)
    EFN = sum(_num(row.get(f"{group_key}_EFN")) for row in rows)
    EV = sum(_num(row.get(f"{group_key}_EV")) for row in rows)
    business_tp_sum = sum(_num(row.get(f"{group_key}_business_tp_sum")) for row in rows)
    tp_like_slots = sum(_num(row.get(f"{group_key}_tp_like_slots")) for row in rows)
    cost_values = [_num(row.get(f"{group_key}_cost_usd")) for row in rows]
    duration_values = [_num(row.get(f"{group_key}_duration_sec")) for row in rows]
    duration_sec_total = sum(duration_values)
    metrics = _metric_values(
        TP=TP,
        EFP=EFP,
        EFN=EFN,
        EV=EV,
        business_tp_sum=business_tp_sum,
        tp_like_slots=tp_like_slots,
        input_tokens=sum(_int(row.get(f"{group_key}_input_tokens")) for row in rows),
        output_tokens=sum(_int(row.get(f"{group_key}_output_tokens")) for row in rows),
        duration_sec=duration_sec_total,
    )
    metrics["cost_total_real_llm"] = round(sum(cost_values), 4) if cost_values else 0.0
    metrics["cost_avg_real_llm_run"] = round(sum(cost_values) / len(rows), 4) if rows else 0.0
    metrics["duration_min"] = round(duration_sec_total / 60, 4) if duration_sec_total else 0.0
    metrics["duration_min_avg"] = round(duration_sec_total / 60 / len(rows), 4) if rows and duration_sec_total else 0.0
    return metrics


def aggregate_run_rows(detail_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_rows = [row for row in detail_rows if row.get("aggregation_selected")]
    all_by_run_name: dict[str, list[dict[str, Any]]] = {}
    for row in detail_rows:
        all_by_run_name.setdefault(str(row.get("run_name") or ""), []).append(row)
    by_run_name: dict[str, list[dict[str, Any]]] = {}
    for row in selected_rows:
        by_run_name.setdefault(str(row.get("run_name") or ""), []).append(row)
    out: list[dict[str, Any]] = []
    for run_name, rows in sorted(by_run_name.items()):
        all_rows = all_by_run_name.get(run_name, rows)
        newest = max(all_rows, key=_row_datetime)
        row: dict[str, Any] = {
            "run_type": newest.get("run_type"),
            "pr_number": newest.get("pr_number"),
            "run_name": run_name,
            "run_date": newest.get("run_date"),
            "run_time": newest.get("run_time"),
            "model": newest.get("model"),
            "document_count": len({str(item.get("document") or "") for item in rows}),
            "run_count": len(all_rows),
            "documents": "; ".join(sorted({str(item.get("document") or "") for item in rows})),
            "source_run_dirs": "; ".join(sorted({str(item.get("run_dir") or "") for item in rows})),
        }
        for group_key, _ in RUN_GROUPS:
            metrics = _aggregate_group(rows, group_key)
            for field in RUN_METRIC_FIELDS:
                row[f"{group_key}_{field}"] = metrics.get(field)

        by_doc: dict[str, list[float]] = {}
        for item in rows:
            by_doc.setdefault(str(item.get("document") or ""), []).append(_num(item.get("overall_f1")))
        deviations: list[float] = []
        stds: list[float] = []
        ranges: list[float] = []
        for values in by_doc.values():
            if len(values) <= 1:
                deviations.append(0.0)
                stds.append(0.0)
                ranges.append(0.0)
                continue
            mean = statistics.fmean(values)
            deviations.append(statistics.fmean(abs(value - mean) for value in values))
            stds.append(statistics.pstdev(values))
            ranges.append(max(values) - min(values))
        row["f1_mean_deviation"] = statistics.fmean(deviations) if deviations else 0.0
        row["f1_std"] = statistics.fmean(stds) if stds else 0.0
        row["f1_range"] = statistics.fmean(ranges) if ranges else 0.0
        out.append(row)
    return sorted(out, key=lambda row: str(row.get("run_name") or ""))


def _write_two_row_header(ws: openpyxl.worksheet.worksheet.Worksheet, columns: list[Col]) -> None:
    ws.append([col.group or col.label for col in columns])
    ws.append([col.label if col.group else "" for col in columns])
    start = 1
    while start <= len(columns):
        group = columns[start - 1].group
        end = start
        while end <= len(columns) and columns[end - 1].group == group:
            end += 1
        end -= 1
        if group:
            if end > start:
                ws.merge_cells(start_row=1, start_column=start, end_row=1, end_column=end)
        else:
            ws.merge_cells(start_row=1, start_column=start, end_row=2, end_column=start)
        start = end + 1


def _format_sheet(ws: openpyxl.worksheet.worksheet.Worksheet, columns: list[Col], freeze: str = "A3") -> None:
    header_fill = PatternFill("solid", fgColor="17324D")
    header_font = Font(bold=True, color="FFFFFF")
    for row in ws.iter_rows(min_row=1, max_row=2):
        for cell in row:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = freeze
    ws.auto_filter.ref = f"A2:{get_column_letter(len(columns))}{ws.max_row}"
    for idx, col in enumerate(columns, start=1):
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = col.width
        if col.hidden_group:
            ws.column_dimensions[letter].outline_level = 1
            ws.column_dimensions[letter].hidden = True
        if col.number_format:
            for cell in ws.iter_cols(min_col=idx, max_col=idx, min_row=3, max_row=ws.max_row):
                for item in cell:
                    item.number_format = col.number_format
    ws.sheet_properties.outlinePr.summaryRight = True


def _write_rows(wb: openpyxl.Workbook, sheet_name: str, rows: list[dict[str, Any]], columns: list[Col], freeze: str = "A3") -> None:
    ws = wb.create_sheet(sheet_name)
    _write_two_row_header(ws, columns)
    for row in rows:
        ws.append([_blank_if_none(row.get(col.key)) for col in columns])
    _format_sheet(ws, columns, freeze=freeze)


def _error_columns() -> list[Col]:
    return [
        Col("run_name", "run_name", 34),
        Col("run_id", "run_id", 36),
        Col("document_run_id", "document_run_id", 36),
        Col("document", "document", 30),
        Col("enum", "enum", 30),
        Col("period", "period", 14),
        Col("segment", "segment", 24),
        Col("status", "status", 10),
        Col("reason", "reason", 32),
        Col("gt_value", "gt_value", 14),
        Col("pred_value", "pred_value", 14),
        Col("gt_unit", "gt_unit", 12),
        Col("pred_unit", "pred_unit", 12),
        Col("business_score", "business_score", 14, "0.00"),
        Col("run_dir", "run_dir", 54),
    ]


def _enum_columns() -> list[Col]:
    return [
        Col("run_name", "run_name", 34),
        Col("run_id", "run_id", 36),
        Col("enum", "enum", 30),
        Col("metric_group", "metric_group", 24),
        *[
            Col(
                field,
                field,
                13,
                "0.00%" if field in {"precision", "recall", "f1", "accuracy", "tp_accuracy"} else "0.00",
            )
            for field in METRIC_FIELDS[:11]
        ],
    ]


def _read_existing_sheet_rows(xlsx_path: Path, sheet_name: str, columns: list[Col]) -> list[dict[str, Any]]:
    if not xlsx_path.exists():
        return []
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    if sheet_name not in wb.sheetnames:
        return []
    ws = wb[sheet_name]
    rows: list[dict[str, Any]] = []
    for values in ws.iter_rows(min_row=3, max_col=len(columns), values_only=True):
        if not values or all(value in {None, ""} for value in values):
            continue
        row = {columns[idx].key: values[idx] if idx < len(values) else None for idx in range(len(columns))}
        rows.append(row)
    return rows


def _load_existing_benchmark_rows(xlsx_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        _read_existing_sheet_rows(xlsx_path, "Подробности", _base_detail_columns(full=True)),
        _read_existing_sheet_rows(xlsx_path, "Ошибки", _error_columns()),
        _read_existing_sheet_rows(xlsx_path, "По метрикам", _enum_columns()),
    )


def _detail_row_key(row: dict[str, Any]) -> tuple[str, ...]:
    run_name = str(row.get("run_name") or "")
    document = str(row.get("document") or "")
    run_dir = str(row.get("run_dir") or row.get("source_group_run_path") or "")
    if run_name and document and run_dir:
        return ("run_dir", run_name, document, run_dir)
    document_run_id = str(row.get("document_run_id") or "")
    if document_run_id:
        return ("document_run_id", document_run_id)
    return (
        "fallback",
        run_name,
        document,
        str(row.get("run_date") or ""),
        str(row.get("run_time") or ""),
        str(row.get("run_id") or ""),
    )


def _generic_row_key(row: dict[str, Any], fields: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "") for field in fields)


def _refresh_duplicate_detail_row(existing_row: dict[str, Any], new_row: dict[str, Any]) -> None:
    for group_key, _ in DETAIL_GROUPS:
        for field in TELEMETRY_METRIC_FIELDS:
            key = f"{group_key}_{field}"
            if not _has_value(existing_row.get(key)) and _has_value(new_row.get(key)):
                existing_row[key] = new_row.get(key)
    for field in ("llm_requests", "classifier_status", "source_run_dirs"):
        if not _has_value(existing_row.get(field)) and _has_value(new_row.get(field)):
            existing_row[field] = new_row.get(field)


def _merge_detail_rows(
    existing_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    *,
    allow_existing_run_name: bool,
) -> list[dict[str, Any]]:
    existing_by_key = {_detail_row_key(row): row for row in existing_rows}
    existing_keys = set(existing_by_key)
    existing_run_names = {str(row.get("run_name") or "") for row in existing_rows if row.get("run_name")}
    conflicts: set[str] = set()
    accepted: list[dict[str, Any]] = []
    for row in new_rows:
        key = _detail_row_key(row)
        if key in existing_keys:
            _refresh_duplicate_detail_row(existing_by_key[key], row)
            continue
        run_name = str(row.get("run_name") or "")
        if run_name in existing_run_names and not allow_existing_run_name:
            conflicts.add(run_name)
            continue
        existing_keys.add(key)
        accepted.append(row)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise BenchmarkHistoryError(
            "Benchmark workbook already contains run_name(s): "
            f"{names}. Use --append-to-existing-run for an intentional rerun/replay with the same name."
        )
    merged = [*existing_rows, *accepted]
    _apply_detail_status_rules(merged)
    return merged


def _merge_unique_rows(
    existing_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    key_fields: Iterable[str],
) -> list[dict[str, Any]]:
    keys = {_generic_row_key(row, key_fields) for row in existing_rows}
    merged = list(existing_rows)
    for row in new_rows:
        key = _generic_row_key(row, key_fields)
        if key in keys:
            continue
        keys.add(key)
        merged.append(row)
    return merged


def _merge_benchmark_rows(
    *,
    out_xlsx: Path,
    new_detail_rows: list[dict[str, Any]],
    new_error_rows: list[dict[str, Any]],
    new_enum_rows: list[dict[str, Any]],
    merge_existing: bool,
    allow_existing_run_name: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if merge_existing:
        existing_detail_rows, existing_error_rows, existing_enum_rows = _load_existing_benchmark_rows(out_xlsx)
    else:
        existing_detail_rows, existing_error_rows, existing_enum_rows = [], [], []
    detail_rows = _merge_detail_rows(
        existing_detail_rows,
        new_detail_rows,
        allow_existing_run_name=allow_existing_run_name,
    )
    error_rows = _merge_unique_rows(
        existing_error_rows,
        new_error_rows,
        ("run_name", "run_id", "document_run_id", "document", "enum", "period", "segment", "status", "run_dir"),
    )
    enum_rows = _merge_unique_rows(
        existing_enum_rows,
        new_enum_rows,
        ("run_name", "run_id", "enum", "metric_group"),
    )
    return detail_rows, error_rows, enum_rows


def _error_rows(run_dirs: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        context = _read_context(run_dir)
        run_name = _legacy_run_name(run_dir, context)
        run_id = str((context.get("benchmark_v2") or {}).get("run_id") or "")
        for row in _read_csv(run_dir / "row_level_comparison.csv"):
            if row.get("status") not in {"EFP", "EFN", "EV"}:
                continue
            out.append(
                {
                    "run_name": run_name,
                    "run_id": run_id,
                    "document_run_id": f"{run_id}:{row.get('document','')}" if run_id else "",
                    "document": row.get("document"),
                    "enum": row.get("enum"),
                    "period": row.get("period"),
                    "segment": row.get("segment"),
                    "status": row.get("status"),
                    "reason": row.get("reasons"),
                    "gt_value": row.get("ref_value"),
                    "pred_value": row.get("pred_value"),
                    "gt_unit": row.get("ref_unit"),
                    "pred_unit": row.get("pred_unit"),
                    "business_score": _num(row.get("business_score")),
                    "run_dir": str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir),
                }
            )
    return out


def _enum_rows(run_dirs: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        context = _read_context(run_dir)
        run_name = _legacy_run_name(run_dir, context)
        run_id = str((context.get("benchmark_v2") or {}).get("run_id") or "")
        for row in _read_csv(run_dir / "metrics_by_enum.csv"):
            if row.get("scope") == "[TOTAL]":
                continue
            metrics = _metrics_from_flat(row)
            out.append(
                {
                    "run_name": run_name,
                    "run_id": run_id,
                    "enum": row.get("scope"),
                    "metric_group": "overall_main_segments",
                    **metrics,
                }
            )
    return out


def _method_rows() -> list[dict[str, Any]]:
    return [
        {"section": "Источник правды", "text": "Подробности: одна строка = один документ в одном конкретном run/replay."},
        {"section": "Прогоны", "text": "Aggregated from latest valid document rows by run_name. Failed, empty, token_limit and invalid rows are not selected into run metrics."},
        {"section": "aggregation_selected", "text": "true означает, что строка Подробностей вошла в текущий агрегат листа Прогоны."},
        {"section": "Стоимость", "text": "Replay rows copy cost and duration from the replaced failed row when run_name and document match. Run cost and duration are calculated over selected valid rows."},
        {"section": "Метрики", "text": "precision=TP/(TP+EFP+EV), recall=TP/(TP+EFN+EV), accuracy=TP/(TP+EFP+EFN+EV), tp_accuracy=business_tp_sum/tp_like_slots."},
        {"section": "Scope предсказаний", "text": "MAIN_STRICT (golden): main-предсказание по метрике, отсутствующей в GT, = галлюцинация = EFP (снижает precision). Сегментные предсказания фильтруются по enum'ам GT (сегментная разметка неполна by design) — отфильтрованное пишется в ignored_predictions_long.csv."},
    ]


def write_benchmark_v2(
    out_xlsx: Path,
    run_dirs: list[Path],
    *,
    merge_existing: bool = True,
    allow_existing_run_name: bool = False,
) -> None:
    new_detail_rows = collect_detail_rows(run_dirs)
    detail_rows, error_rows, enum_rows = _merge_benchmark_rows(
        out_xlsx=out_xlsx,
        new_detail_rows=new_detail_rows,
        new_error_rows=_error_rows(run_dirs),
        new_enum_rows=_enum_rows(run_dirs),
        merge_existing=merge_existing,
        allow_existing_run_name=allow_existing_run_name,
    )
    select_aggregation_rows(detail_rows)
    run_rows = aggregate_run_rows(detail_rows)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _write_rows(wb, "Прогоны", run_rows, _run_columns(), freeze="A3")
    _write_rows(wb, "Детали", detail_rows, _base_detail_columns(full=False), freeze="A3")
    _write_rows(wb, "Подробности", detail_rows, _base_detail_columns(full=True), freeze="A3")
    _write_rows(
        wb,
        "Ошибки",
        error_rows,
        _error_columns(),
        freeze="A3",
    )
    _write_rows(
        wb,
        "По метрикам",
        enum_rows,
        _enum_columns(),
        freeze="A3",
    )
    _write_rows(
        wb,
        "Методика",
        _method_rows(),
        [Col("section", "section", 24), Col("text", "text", 120)],
        freeze="A3",
    )
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_xlsx)


def _write_benchmark_v2_logged(
    out_xlsx: Path,
    run_dirs: list[Path],
    label: str,
    *,
    merge_existing: bool = True,
    allow_existing_run_name: bool = False,
) -> bool:
    try:
        write_benchmark_v2(
            out_xlsx,
            run_dirs,
            merge_existing=merge_existing,
            allow_existing_run_name=allow_existing_run_name,
        )
    except BenchmarkHistoryError as exc:
        _log(f"ERROR: {exc}")
        raise
    except PermissionError:
        _log(
            f"WARNING: could not write {label}: {out_xlsx}. "
            "File is probably open or locked; close it and rerun --mode collect."
        )
        return False
    _log(f"Wrote {label}: {out_xlsx}; imported run dirs: {len(run_dirs)}")
    return True


def _safe_path_part(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", str(value or "").strip())
    return text.strip("._-") or "run"


def _write_failed_case_run(
    *,
    args: argparse.Namespace,
    case: BenchmarkCase,
    effective_run_name: str,
    repeat_idx: int,
    repeats: int,
    variant: PromptSuiteVariant,
    pdf_filters: list[str],
    elapsed: float,
    exc: Exception,
) -> Path:
    doc = pdf_filters[0] if len(pdf_filters) == 1 else ",".join(pdf_filters) or case.name
    stamp = datetime.now().strftime("%d_%m_%y_%H_%M")
    run_dir = args.runs_dir / _safe_path_part(f"{effective_run_name}_{stamp}_{Path(doc).stem}_failed")
    run_dir.mkdir(parents=True, exist_ok=True)
    error_text = f"{type(exc).__name__}: {exc}"
    context = {
        "pipeline_profile": "benchmark_v2_failed_case",
        "dataset": case.dataset,
        "dataset_items": case.dataset_name,
        "gt_xlsx": str(case.gt_xlsx),
        "input_mode": args.input_mode or case.input_mode,
        "prompt_text_mode": variant.prompt_text_mode,
        "table_context_lines": str(variant.table_context_lines),
        "ocr_json_variant": args.ocr_json_variant,
        "inputs": ";".join(str(path) for path in case.json_dirs),
        "input_paths": ";".join(str(path) for path in case.json_dirs),
        "pdf_filters": ",".join(pdf_filters) or "<all>",
        "allow_llm": str(args.allow_llm),
        "include_segments": str(case.include_segments if args.include_segments_override is None else args.include_segments_override),
        "include_consolidated_group": str(case.include_consolidated_group),
        "segment_mode": str(args.segment_mode or case.segment_mode),
        "segment_context_mode": str(args.segment_context_mode or case.segment_context_mode),
        "metrics_from_gt": str(case.metrics_from_gt),
        "metric_scope": str(args.metric_scope if args.metric_scope is not None else case.metric_scope or "full"),
        "score_metric_scope": str(args.score_metric_scope if args.score_metric_scope is not None else case.score_metric_scope or "full"),
        "extractor_matrix": str(args.extractor_matrix or case.extractor_matrix),
        "company_type": case.company_type,
        "role": case.role or "",
        "model_id": args.model_id or "",
        "model_id_requested": args.model_id or "",
        "jobs_requested": str(args.jobs),
        "jobs_effective": "0",
        "llm_request_count": "0",
        "llm_requests_cost_usd": "0",
        "llm_requests_elapsed_s": str(round(elapsed, 1)),
        "llm_requests_tokens_in": "0",
        "llm_requests_tokens_out": "0",
        "error": error_text,
        "benchmark_v2": {
            "schema": "benchmark_v2",
            "run_id": uuid4().hex,
            "run_name": effective_run_name,
            "base_run_name": args.run_name,
            "run_type": args.run_type,
            "repeat_no": repeat_idx,
            "repeat_count": repeats,
            "prompt_suite": args.prompt_suite,
            "prompt_variant": variant.key,
            "prompt_variant_title": variant.title,
        },
    }
    metric_headers = [
        "scope", "TP", "EFP", "EFN", "EV", "TN", "duplicates", "business_tp_sum",
        "pred_positive_count", "ref_positive_count", "tp_like_slots", "precision",
        "recall", "f1", "f05", "accuracy", "business_precision", "business_recall",
        "business_f1", "business_f05", "business_utility",
    ]
    _write_csv(
        run_dir / "metrics_by_document.csv",
        [{
            "scope": doc,
            **{key: 0 for key in metric_headers if key != "scope"},
        }],
        metric_headers,
    )
    _write_csv(run_dir / "row_level_comparison.csv", [], ["document", "enum", "period", "segment", "status", "reason"])
    _write_csv(
        run_dir / "run_records.csv",
        [{
            "dataset": case.dataset_name,
            "mode": "real" if args.allow_llm else "dry",
            "input_mode": args.input_mode or case.input_mode,
            "pdf_name": doc,
            "prediction_rows": 0,
            "error": error_text,
            "elapsed_s": round(elapsed, 1),
            "company_type": case.company_type,
        }],
        ["dataset", "mode", "input_mode", "pdf_name", "prediction_rows", "error", "elapsed_s", "company_type"],
    )
    _write_json(run_dir / "run_context.json", context)
    return run_dir


async def run_cases_v2(args: argparse.Namespace) -> list[Path]:
    if args.reasoning_effort:
        from finengine.llm.openrouter import set_reasoning_effort

        set_reasoning_effort(args.reasoning_effort)
        _log(f"OpenRouter reasoning effort: {args.reasoning_effort}")
    git_meta = _git_metadata()
    run_dirs: list[Path] = []
    raw_pdf_filters = _parse_pdf_args(args.pdf)
    unmatched_filters = set(raw_pdf_filters)
    slots: list[RunSlot] = []
    for variant in _prompt_suite_variants(args):
        effective_run_name = _effective_run_name(args.run_name, variant, args.prompt_suite)
        for case in _selected_cases_v2(args):
            repeats = max(1, int(args.repeat or case.repeat or 1))
            if raw_pdf_filters and case.suite != "golden":
                documents = _read_gt_documents(case.gt_xlsx)
                matched_filters = list(_match_pdf_filters(documents, raw_pdf_filters))
                if not matched_filters:
                    continue
                for matched in matched_filters:
                    matched_key = matched.lower()
                    for raw in raw_pdf_filters:
                        raw_key = raw.lower()
                        if raw_key == matched_key or raw_key in matched_key or matched_key in raw_key:
                            unmatched_filters.discard(raw)
                pdf_filters = matched_filters
            else:
                pdf_filters = list(case.pdf_filters)
                if raw_pdf_filters and case.suite == "golden":
                    for matched in pdf_filters:
                        matched_key = matched.lower()
                        stem_key = Path(matched).stem.lower()
                        for raw in raw_pdf_filters:
                            raw_key = raw.lower()
                            if raw_key == matched_key or raw_key == stem_key or raw_key in matched_key or raw_key in stem_key:
                                unmatched_filters.discard(raw)
            _log(
                "case selected: "
                f"run_name={effective_run_name}; "
                f"case={case.name}; pdfs={', '.join(pdf_filters) if pdf_filters else '<case default/all>'}; "
                f"input_mode={args.input_mode or case.input_mode}; "
                f"prompt_suite={args.prompt_suite}; "
                f"prompt_variant={variant.key}; "
                f"prompt_text_mode={variant.prompt_text_mode}; "
                f"table_context_lines={variant.table_context_lines}; "
                f"ocr_json_variant={args.ocr_json_variant}; "
                f"jobs={args.jobs}",
                level="debug",
            )
            for repeat_idx in range(1, repeats + 1):
                slots.append(
                    RunSlot(
                        variant=variant,
                        effective_run_name=effective_run_name,
                        case=case,
                        pdf_filters=tuple(pdf_filters),
                        repeat_idx=repeat_idx,
                        repeats=repeats,
                    )
                )
    if raw_pdf_filters and unmatched_filters:
        _log(f"Warning: these --pdf filters did not match selected GT documents: {', '.join(sorted(unmatched_filters))}")
    total_slots = len(slots)
    slots = [
        RunSlot(
            variant=slot.variant,
            effective_run_name=slot.effective_run_name,
            case=slot.case,
            pdf_filters=slot.pdf_filters,
            repeat_idx=slot.repeat_idx,
            repeats=slot.repeats,
            slot_no=index,
            total_slots=total_slots,
        )
        for index, slot in enumerate(slots, start=1)
    ]
    if raw_pdf_filters and not slots and not args.dry_run:
        raise RuntimeError("No selected benchmark case matched the provided --pdf filters.")
    if args.dry_run:
        for slot in slots:
            _log(
                f"queued [{slot.slot_no}/{slot.total_slots}] {slot.case.name} "
                f"({', '.join(slot.pdf_filters) if slot.pdf_filters else '<case default/all>'})"
            )
            _log(f"{slot.effective_run_name}: [{slot.slot_no}/{slot.total_slots}] dry-run planned, LLM not called")
        _log(f"dry-run complete; planned run slots: {total_slots}")
        return run_dirs

    for slot in slots:
        _log(
            f"queued [{slot.slot_no}/{slot.total_slots}] {slot.case.name} "
            f"({', '.join(slot.pdf_filters) if slot.pdf_filters else '<case default/all>'})"
        )

    jobs = max(1, int(args.jobs or 1))
    sem = asyncio.Semaphore(jobs)
    state = {"running": 0}
    state_lock = asyncio.Lock()

    async def _run_slot(slot: RunSlot) -> Path:
        case = slot.case
        variant = slot.variant
        pdf_filters = list(slot.pdf_filters)
        label = f"{slot.effective_run_name}: [{slot.slot_no}/{slot.total_slots}] {case.name}"
        async with sem:
            start = asyncio.get_running_loop().time()
            async with state_lock:
                state["running"] += 1
                running = state["running"]
            _log(
                f"running [{slot.slot_no}/{slot.total_slots}] {case.name} "
                f"({running}/{jobs} active)"
            )
            _log(f"{label}: started", level="debug")
            progress_state = {"message": "starting benchmark case"}

            def _set_progress(message: str) -> None:
                cleaned = " ".join(str(message).split())
                if not cleaned or progress_state.get("message") == cleaned:
                    return
                progress_state["message"] = cleaned
                _log(f"{label}: {cleaned}", level="debug")

            heartbeat = None
            if _VERBOSITY == "debug":
                heartbeat = asyncio.create_task(_heartbeat(label, start, progress_state))
            try:
                inner_jobs = args.jobs if slot.total_slots == 1 else 1
                run = await run_finengine_e2e_quality(
                    dataset=case.dataset,
                    pdf_filters=pdf_filters,
                    gt_kind=case.gt_kind,
                    gt_xlsx=case.gt_xlsx,
                    json_dirs=list(case.json_dirs),
                    pdf_dirs=list(case.pdf_dirs),
                    input_mode=args.input_mode or case.input_mode,
                    prompt_text_mode=variant.prompt_text_mode,
                    force_idp_ocr=args.force_idp_ocr,
                    table_context_lines=variant.table_context_lines,
                    ocr_json_variant=args.ocr_json_variant,
                    jobs=inner_jobs,
                    dataset_name=case.dataset_name,
                    allow_llm=args.allow_llm,
                    include_segments=(
                        case.include_segments
                        if args.include_segments_override is None
                        else args.include_segments_override
                    ),
                    include_consolidated_group=case.include_consolidated_group,
                    model_id=args.model_id,
                    limit=args.limit or None,
                    max_docs=args.max_docs,
                    out_dir=args.runs_dir,
                    metrics_from_gt=case.metrics_from_gt,
                    metric_scope=args.metric_scope if args.metric_scope is not None else case.metric_scope,
                    score_metric_scope=args.score_metric_scope if args.score_metric_scope is not None else case.score_metric_scope,
                    real_run_attempts=args.real_run_attempts if args.real_run_attempts > 0 else case.real_run_attempts,
                    extractor_matrix=args.extractor_matrix or case.extractor_matrix,
                    company_type=case.company_type,
                    role=case.role,
                    run_name=slot.effective_run_name,
                    replay_events=args.replay_events,
                    unit_mode=case.unit_mode,
                    period_key_mode=case.period_key_mode,
                    prediction_scope=case.prediction_scope,
                    segment_mode=args.segment_mode or case.segment_mode,
                    segment_context_mode=args.segment_context_mode or case.segment_context_mode,
                    legacy_replay=args.legacy_replay,
                    progress_callback=_set_progress,
                )
            except Exception as exc:
                elapsed = asyncio.get_running_loop().time() - start
                _log(f"{label}: failed in {_format_elapsed(elapsed)}; {type(exc).__name__}: {exc}")
                return _write_failed_case_run(
                    args=args,
                    case=case,
                    effective_run_name=slot.effective_run_name,
                    repeat_idx=slot.repeat_idx,
                    repeats=slot.repeats,
                    variant=variant,
                    pdf_filters=pdf_filters,
                    elapsed=elapsed,
                    exc=exc,
                )
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                async with state_lock:
                    state["running"] -= 1

            elapsed = asyncio.get_running_loop().time() - start
            context = _read_context(run.run_dir)
            _log(
                f"{label}: completed in {_format_elapsed(elapsed)}; "
                f"{_format_quality_metric(run.result.micro)}; {_format_usage(context)}"
            )
            for document, metric in sorted(run.result.by_document.items()):
                _log(f"{label}: document {document}: {_format_quality_metric(metric)}", level="debug")
            context["benchmark_v2"] = {
                "schema": "benchmark_v2",
                "run_id": uuid4().hex,
                "run_name": slot.effective_run_name,
                "base_run_name": args.run_name,
                "run_type": args.run_type,
                "repeat_no": slot.repeat_idx,
                "repeat_count": slot.repeats,
                "prompt_suite": args.prompt_suite,
                "prompt_variant": variant.key,
                "prompt_variant_title": variant.title,
                "prompt_text_mode": variant.prompt_text_mode,
                "table_context_lines": variant.table_context_lines,
                "ocr_json_variant": args.ocr_json_variant,
                "source_kind": "new_runner",
                **git_meta,
            }
            _write_json(run.run_dir / "run_context.json", context)
            _log(f"{label}: run_dir={run.run_dir}", level="debug")
            return run.run_dir

    tasks = [asyncio.create_task(_run_slot(slot)) for slot in slots]
    for task in asyncio.as_completed(tasks):
        run_dirs.append(await task)
    return run_dirs


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["collect", "run"], default="collect")
    parser.add_argument("--verbosity", choices=sorted(VERBOSITY_LEVELS), default="normal")
    parser.add_argument("--suite", choices=["main", "full", "segments", "banks_ru", "golden", "all"], default="all")
    parser.add_argument("--case", action="append", default=[], help="Specific case name; can be repeated")
    parser.add_argument(
        "--golden-basket",
        action="append",
        default=[],
        choices=sorted(GOLDEN_BASKETS),
        help="Run a tests/test_benchmark/data/golden/<basket> basket through benchmark v2. Can be repeated.",
    )
    parser.add_argument(
        "--golden-case",
        action="append",
        default=[],
        choices=sorted(GOLDEN_CASES),
        help="Named golden document subset. Can be repeated; use with --suite golden.",
    )
    parser.add_argument("--golden-root", type=Path, default=GOLDEN_ROOT)
    parser.add_argument("--run-name", default="", help="Required in --mode run")
    parser.add_argument("--run-type", choices=["test", "control"], default="", help="Required in --mode run")
    parser.add_argument("--allow-llm", action="store_true")
    parser.add_argument("--input-mode", choices=["json", "pdf"], default=None)
    parser.add_argument("--pdf", action="append", default=[])
    parser.add_argument(
        "--prompt-suite",
        choices=["single", "ocr_ablation"],
        default="single",
        help="single keeps one prompt mode; ocr_ablation runs five OCR prompt assembly variants.",
    )
    parser.add_argument("--prompt-text-mode", choices=sorted(PROMPT_TEXT_MODES), default="default")
    parser.add_argument("--force-idp-ocr", action="store_true")
    parser.add_argument("--table-context-lines", type=int, default=2)
    parser.add_argument("--ocr-json-variant", choices=sorted(OCR_JSON_VARIANTS), default="old")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true", help="Print the run plan without calling the LLM")
    parser.add_argument("--replay-events", type=Path, default=None)
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-docs", type=int, default=0)
    parser.add_argument("--metric-scope", default=None)
    parser.add_argument("--score-metric-scope", default=None)
    parser.add_argument("--role", default="", help="Role override for golden basket runs; defaults to other")
    parser.add_argument("--extractor-matrix", action="store_true")
    parser.add_argument("--include-segments", dest="include_segments_override", action="store_true")
    parser.add_argument("--no-segments", dest="include_segments_override", action="store_false")
    parser.set_defaults(include_segments_override=None)
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--real-run-attempts", type=int, default=0)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--source-runs", type=Path, action="append", default=[])
    parser.add_argument(
        "--append",
        "--append-to-existing-run",
        "--allow-existing-run-name",
        dest="append_to_existing_run",
        action="store_true",
        help="Allow importing new rows with a run_name already present in the benchmark workbook.",
    )
    parser.add_argument("--since", default="", help="ISO date, for collect mode legacy import filtering")
    parser.add_argument("--out-xlsx", type=Path, default=None, help="Additional per-launch or filtered workbook")
    parser.add_argument("--segment-mode", choices=[mode.value for mode in SegmentMode], default=SegmentMode.EXTENDED.value)
    parser.add_argument("--segment-context-mode", choices=["full", "focused"], default="full")
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        default="",
        help="OpenRouter reasoning effort for this benchmark run. Use 'none' to omit the reasoning block.",
    )
    parser.add_argument("--legacy-replay", action="store_true")
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    global _VERBOSITY
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _VERBOSITY = args.verbosity
    if args.mode == "run" and not args.run_name:
        parser.error("--run-name is required in --mode run")
    if args.mode == "run" and not args.run_type:
        parser.error("--run-type test|control is required in --mode run")
    if args.mode == "run" and not args.allow_llm and not args.replay_events and not args.dry_run:
        parser.error("running cases calls the LLM unless --replay-events is supplied; pass --allow-llm explicitly")
    try:
        _preflight_existing_run_names(args)
    except BenchmarkHistoryError as exc:
        parser.error(str(exc))
    if args.mode == "run":
        current_run_dirs = await run_cases_v2(args)
        _log(f"current launch completed run dirs: {len(current_run_dirs)}")
        if args.dry_run:
            return 0
    else:
        current_run_dirs = []

    if args.mode == "run":
        fixed_import_dirs = current_run_dirs
        _log(f"importing current launch into fixed benchmark: {len(fixed_import_dirs)} run dirs")
    else:
        roots = [*args.source_runs] if args.source_runs else [args.runs_dir]
        _log(f"collecting benchmark imports from: {', '.join(str(root) for root in roots)}")
        fixed_import_dirs = _find_run_dirs(roots, since=args.since or None)
        _log(f"importing discovered run dirs into fixed benchmark ledger: {len(fixed_import_dirs)}")
    _write_benchmark_v2_logged(
        DEFAULT_BENCHMARK_XLSX,
        fixed_import_dirs,
        "fixed benchmark",
        merge_existing=True,
        allow_existing_run_name=args.append_to_existing_run,
    )

    if args.out_xlsx:
        if args.mode == "run" and args.run_name:
            requested_run_names = _requested_run_names(args)
            roots = [args.runs_dir, *args.source_runs]
            all_run_dirs = _find_run_dirs(roots, since=args.since or None)
            named_dirs = [
                run_dir
                for run_dir in all_run_dirs
                if _legacy_run_name(run_dir, _read_context(run_dir)) in requested_run_names
            ]
            export_dirs = named_dirs or current_run_dirs
        elif args.source_runs:
            export_dirs = fixed_import_dirs
        else:
            export_dirs = fixed_import_dirs
        _write_benchmark_v2_logged(
            args.out_xlsx,
            export_dirs,
            "additional benchmark",
            merge_existing=args.out_xlsx.exists(),
            allow_existing_run_name=args.append_to_existing_run,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
