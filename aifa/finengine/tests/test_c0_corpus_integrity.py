from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
MANIFEST_PATH = GOLDEN / "c0_manifest.json"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_c0_readers_find_the_required_case_counts():
    extraction = sorted((GOLDEN / "extraction" / "cases").glob("*.json"))
    structures = sorted((GOLDEN / "structures" / "cases").glob("*.json"))
    analysis = sorted((GOLDEN / "analysis" / "cases").glob("*.json"))
    facts = sum(len(_json(path)["facts"]) for path in extraction)

    assert len(extraction) == 30
    assert facts == 300
    assert len(structures) == 20
    assert len(analysis) == 30


def test_c0_document_and_case_hashes_are_reproducible():
    manifest = _json(MANIFEST_PATH)
    assert manifest["schema_version"] == "c0.1-existing-schema"
    assert manifest["generation"]["llm_calls"] == 0

    for relative_path, expected in manifest["case_file_sha256"].items():
        assert _sha256(ROOT / relative_path) == expected
    for report in manifest["reports"]:
        assert _sha256(ROOT / report["pdf_path"]) == report["document_sha256"]


def test_c0_extraction_facts_have_expected_value_evidence_and_period():
    manifest = _json(MANIFEST_PATH)
    reports = {report["report_id"]: report for report in manifest["reports"]}

    for case_path in sorted((GOLDEN / "extraction" / "cases").glob("*.json")):
        case = _json(case_path)
        assert case["source_url"].startswith("https://")
        report = reports[case["report_id"]]
        assert case["document_version"] == report["document_sha256"]
        ocr = _json(ROOT / report["ocr_path"])["ocr_result"]
        page_tables: dict[int, list[str]] = {}
        for table in ocr["tables"]:
            page_tables.setdefault(int(table["page"]), []).append(
                str(table.get("original_text") or table.get("text") or "")
            )
        for fact in case["facts"]:
            assert fact["fact_id"]
            assert fact["concept"]
            assert isinstance(fact["value"], (int, float))
            assert fact["unit"]
            assert fact["period_ref"]
            assert fact["basis"]
            assert fact["page"] > 0
            assert fact["quote"]
            assert any(fact["quote"] in table for table in page_tables[fact["page"]])


def test_c0_structures_and_analysis_reference_the_extraction_evidence():
    evidence_ids = {
        fact["fact_id"]
        for path in (GOLDEN / "extraction" / "cases").glob("*.json")
        for fact in _json(path)["facts"]
    }
    for path in (GOLDEN / "structures" / "cases").glob("*.json"):
        structure = _json(path)
        assert structure["structure_id"]
        assert structure["table_kind"]
        assert structure["page"] > 0
        assert structure["rows"]
    for path in (GOLDEN / "analysis" / "cases").glob("*.json"):
        case = _json(path)
        assert case["evidence_ids"]
        assert set(case["evidence_ids"]) <= evidence_ids
        assert case["expected_judgment"]["concept"]
        assert case["expected_explanation_covers"]
        assert case["rubric"]


def test_c0_generated_metadata_contains_no_embedded_secrets():
    secret_assignment = re.compile(
        r"(?i)(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*['\"][^'\"]{8,}"
    )
    for path in [MANIFEST_PATH, *GOLDEN.glob("*/cases/*.json")]:
        assert secret_assignment.search(path.read_text(encoding="utf-8")) is None, path
