"""Build the C0 golden corpus from the restored, immutable vendor snapshot.

The builder does not call an LLM and does not change production contracts.  It
joins the existing vendor GT rows to literal numeric rows in the vendor OCR
tables, records the source PDF hash, and emits deterministic JSON fixtures for
the three schemas already documented under ``tests/golden``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = (
    ROOT
    / "benchmarks"
    / "vendor"
    / "tests"
    / "test_benchmark"
    / "data"
    / "golden"
)
EXTRACTION_ROOT = ROOT / "tests" / "golden" / "extraction" / "cases"
STRUCTURES_ROOT = ROOT / "tests" / "golden" / "structures" / "cases"
ANALYSIS_ROOT = ROOT / "tests" / "golden" / "analysis" / "cases"
MANIFEST_PATH = ROOT / "tests" / "golden" / "c0_manifest.json"

UPSTREAM_URL = "https://gitverse.ru/nero27/AIFA_RFA"
UPSTREAM_COMMIT = "383b6aeee240e6304ef932601d7cb86833d21728"
REPORT_COUNT = 30
FACT_COUNT = 300
STRUCTURE_COUNT = 20
ANALYSIS_COUNT = 30


@dataclass(frozen=True)
class SourceSet:
    profile: str
    report_id: str
    csv_path: Path
    json_path: Path
    pdf_path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sources() -> list[SourceSet]:
    found: list[SourceSet] = []
    for csv_path in sorted(VENDOR_ROOT.glob("*/gt/*.csv")):
        profile = csv_path.parent.parent.name
        report_id = csv_path.stem
        json_path = csv_path.parent.parent / "json" / f"{report_id}.json"
        pdf_path = csv_path.parent.parent / "pdf" / f"{report_id}.pdf"
        if json_path.is_file() and pdf_path.is_file():
            found.append(SourceSet(profile, report_id, csv_path, json_path, pdf_path))
    return found


def _decimal(value: str) -> Decimal | None:
    text = value.strip().replace("\u00a0", " ").replace("−", "-").replace("–", "-")
    text = text.replace("**", "").replace("%", "")
    text = re.sub(r"^[€$£₹]", "", text).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.replace(" ", "")
    if not re.fullmatch(r"[-+]?\d[\d,.]*", text):
        return None
    if "," in text and "." not in text:
        groups = text.lstrip("-+").split(",")
        text = "".join(groups) if all(len(group) == 3 for group in groups[1:]) else text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return -number if negative else number


def _matching_row(table_text: str, expected: Decimal) -> str | None:
    for raw_line in table_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", ":", " "}:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if any(_decimal(cell) == expected for cell in cells):
            return line
    return None


def _basis(profile: str) -> str:
    if profile.startswith("ifrs_"):
        return "IFRS"
    if profile.startswith("us_gaap_"):
        return "US GAAP"
    if profile.startswith("ind_gaap_"):
        return "Ind AS"
    if profile.startswith("rsbu_"):
        return "RSBU"
    return profile.upper()


def _language(profile: str) -> str:
    return "ru" if profile.endswith("_ru") else "en"


def _company(report_id: str) -> str:
    return re.sub(r"-(?:Q[1-4]|[0-9]{4}|12M).*$", "", report_id).replace("-", " ").title()


def _candidate(source: SourceSet) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    raw_vendor = source.json_path.read_text(encoding="utf-8")
    if raw_vendor.startswith("version https://git-lfs.github.com/spec/v1"):
        return None
    vendor = json.loads(raw_vendor)
    tables = {
        int(table["idx"]): table
        for table in vendor.get("ocr_result", {}).get("tables", [])
        if table.get("idx") is not None and table.get("page") is not None
    }
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    with source.csv_path.open(encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(4096)
        stream.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        for row_number, row in enumerate(csv.DictReader(stream, dialect=dialect), start=2):
            concept = (row.get("enum") or "").strip()
            period = (row.get("period") or "").strip()
            unit = (row.get("unit") or "").strip()
            raw_value = (row.get("value") or "").strip()
            raw_table = (row.get("table") or "").strip()
            if not concept or not period or not unit or not raw_table:
                continue
            value = _decimal(raw_value)
            if value is None:
                continue
            table_matches: list[tuple[int, dict[str, Any], str]] = []
            for token in re.split(r"[^0-9]+", raw_table):
                if not token:
                    continue
                table_index = int(token)
                table = tables.get(table_index)
                if table is None:
                    continue
                quote = _matching_row(
                    str(table.get("original_text") or table.get("text") or ""), value
                )
                if quote is not None:
                    table_matches.append((table_index, table, quote))
            if not table_matches:
                continue
            table_index, table, quote = table_matches[0]
            identity = (concept, period, str(value))
            if identity in seen:
                continue
            seen.add(identity)
            facts.append(
                {
                    "basis": _basis(source.profile),
                    "concept": concept,
                    "fact_id": f"{source.report_id}:{concept}:{len(facts) + 1:02d}",
                    "page": int(table["page"]),
                    "period_ref": period,
                    "quote": quote,
                    "unit": unit,
                    "value": float(value),
                    "_vendor_table_index": table_index,
                    "_vendor_gt_row": row_number,
                }
            )
    if not facts:
        return None
    extraction = {
        "company": _company(source.report_id),
        "document_version": _sha256(source.pdf_path),
        "facts": [
            {key: value for key, value in fact.items() if not key.startswith("_vendor_")}
            for fact in facts
        ],
        "language": _language(source.profile),
        "report_id": source.report_id,
        "source_url": UPSTREAM_URL,
    }
    return extraction, facts


def build() -> dict[str, Any]:
    candidates: list[tuple[SourceSet, dict[str, Any], list[dict[str, Any]]]] = []
    for source in _sources():
        candidate = _candidate(source)
        if candidate is not None:
            extraction, internal_facts = candidate
            candidates.append((source, extraction, internal_facts))
    candidates.sort(key=lambda item: (-len(item[2]), item[0].profile, item[0].report_id))
    selected = candidates[:REPORT_COUNT]
    if len(selected) != REPORT_COUNT:
        raise RuntimeError(f"only {len(selected)} source reports have verifiable facts")
    if sum(len(item[2]) for item in selected) < FACT_COUNT:
        raise RuntimeError("selected reports do not contain 300 verifiable facts")

    allocations = [1] * REPORT_COUNT
    remaining = FACT_COUNT - REPORT_COUNT
    while remaining:
        progressed = False
        for index, (_, _, facts) in enumerate(selected):
            if allocations[index] < len(facts):
                allocations[index] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
        if not progressed:
            raise RuntimeError("could not allocate 300 facts across 30 reports")

    generated_paths: list[Path] = []
    report_manifest: list[dict[str, Any]] = []
    all_evidence: set[str] = set()
    for ordinal, (source, extraction, internal_facts) in enumerate(selected):
        internal_facts = internal_facts[: allocations[ordinal]]
        extraction["facts"] = extraction["facts"][: allocations[ordinal]]
        extraction_path = EXTRACTION_ROOT / f"{source.report_id}.json"
        _write_json(extraction_path, extraction)
        generated_paths.append(extraction_path)
        all_evidence.update(fact["fact_id"] for fact in extraction["facts"])

        if ordinal < STRUCTURE_COUNT:
            first_table = internal_facts[0]["_vendor_table_index"]
            table_facts = [
                fact for fact in internal_facts if fact["_vendor_table_index"] == first_table
            ]
            if len(table_facts) < 2:
                table_facts = internal_facts[: min(5, len(internal_facts))]
            structure = {
                "company": extraction["company"],
                "page": table_facts[0]["page"],
                "rows": [
                    {
                        "concept": fact["concept"],
                        "period_ref": fact["period_ref"],
                        "quote": fact["quote"],
                        "unit": fact["unit"],
                        "value": float(fact["value"]),
                    }
                    for fact in table_facts
                ],
                "source_url": UPSTREAM_URL,
                "structure_id": f"{source.report_id}:table:{first_table}",
                "table_kind": "financial_statement",
            }
            structure_path = STRUCTURES_ROOT / f"{source.report_id}-table-{first_table}.json"
            _write_json(structure_path, structure)
            generated_paths.append(structure_path)

        fact = extraction["facts"][0]
        analysis = {
            "case_id": f"{source.report_id}:analysis:01",
            "company": extraction["company"],
            "evidence_ids": [fact["fact_id"]],
            "expected_explanation_covers": [
                fact["concept"],
                fact["period_ref"],
                fact["unit"],
                "source evidence",
            ],
            "expected_judgment": {
                "concept": fact["concept"],
                "verdict": "reported_as_disclosed",
            },
            "goal": (
                f"State the disclosed {fact['concept']} for {fact['period_ref']} "
                "and cite the supplied evidence. Do not add a recommendation."
            ),
            "rubric": {
                "evidence_alignment": "required: every number traces to evidence_ids",
                "factual_accuracy": "required: value, period, and unit match the evidence",
                "logical_coherence": "required: no claim contradicts the evidence",
                "no_recommendations": "required",
            },
        }
        analysis_path = ANALYSIS_ROOT / f"{source.report_id}.json"
        _write_json(analysis_path, analysis)
        generated_paths.append(analysis_path)

        report_manifest.append(
            {
                "document_sha256": extraction["document_version"],
                "fact_count": len(extraction["facts"]),
                "gt_path": source.csv_path.relative_to(ROOT).as_posix(),
                "ocr_path": source.json_path.relative_to(ROOT).as_posix(),
                "pdf_path": source.pdf_path.relative_to(ROOT).as_posix(),
                "profile": source.profile,
                "report_id": source.report_id,
            }
        )

    case_hashes = {
        path.relative_to(ROOT).as_posix(): _sha256(path)
        for path in sorted(generated_paths)
    }
    manifest = {
        "analysis_cases": ANALYSIS_COUNT,
        "case_file_sha256": case_hashes,
        "extraction_facts": len(all_evidence),
        "extraction_reports": len(selected),
        "generation": {
            "llm_calls": 0,
            "method": "vendor GT row joined to an exact numeric row in the same vendor OCR table",
            "owner_decision": "machine-derived candidate corpus authorized on 2026-09-22",
            "review_status": "machine_verified_pending_human_fact_review",
        },
        "reports": report_manifest,
        "schema_version": "c0.1-existing-schema",
        "structures_tables": STRUCTURE_COUNT,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_url": UPSTREAM_URL,
    }
    _write_json(MANIFEST_PATH, manifest)
    return manifest


if __name__ == "__main__":
    result = build()
    print(
        f"generated {result['extraction_reports']} reports / "
        f"{result['extraction_facts']} facts / "
        f"{result['structures_tables']} structures / "
        f"{result['analysis_cases']} analysis cases"
    )
