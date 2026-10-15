from __future__ import annotations

import pytest
from pydantic import ValidationError

from finengine.contracts import (
    EvidenceBundleV2,
    FormOutput,
    FormRow,
    NarrativeOutput,
    NumericClaimRecord,
    ScalarFact,
    VerificationFinding,
)
from finengine.skills.atoms.out_report import build_report_output


def _finding(status: str) -> VerificationFinding:
    return VerificationFinding(
        produced_by_atom="verify.grounding",
        produced_by_atom_version="1.0.0",
        source_ref="claim:1",
        check_kind="grounding",
        status=status,
    )


def test_v0_form_payload_is_read_but_serialized_as_v1():
    form = FormOutput.model_validate({
        "output_id": "old-form",
        "run_id": "run-1",
        "role": "dgr",
        "rows": [
            {"kind": "metric", "enum": "REVENUE", "label": "Выручка", "value": 10.0, "available": True},
            {"kind": "metric", "enum": "RWA", "label": "RWA", "value": None, "available": False},
        ],
    })

    wire = form.model_dump(mode="json")
    assert wire["schema_version"] == "1.0"
    assert wire["output_type"] == "FormOutput"
    assert [row["order"] for row in wire["rows"]] == [0, 1]
    assert "available" not in wire["rows"][0]
    assert wire["rows"][1]["status"] == "missing"


def test_form_contract_rejects_missing_row_without_explicit_reason():
    with pytest.raises(ValidationError, match="explain why"):
        FormRow(
            row_id="dgr.bank:001", order=1, kind="dgr_row", enum="RWA",
            label="RWA", status="missing",
        )


def test_report_contains_resolvable_grounding_refs_and_gate_status():
    fact = ScalarFact(
        evidence_id="ev-revenue",
        produced_by_atom="read.value",
        produced_by_atom_version="1.0.0",
        source_ref="doc-1#p7",
        document_version="doc-v2",
        concept="REVENUE",
        period_ref="2025",
        value=125.0,
        unit="RUB_MLN",
    )
    narrative = NarrativeOutput(
        output_id="narrative-1",
        run_id="run-1",
        text="Выручка — 125 млн руб.",
        cited_evidence_ids=[fact.evidence_id],
        claims=[NumericClaimRecord(
            text="Выручка — 125 млн руб.", kind="financial", value=125.0,
            period_ref="2025", unit="RUB_MLN", evidence_id=fact.evidence_id,
        )],
    )
    report = build_report_output(
        run_id="run-1",
        narrative=narrative,
        judgments=[],
        explanations=[],
        bundle=EvidenceBundleV2(run_id="run-1", scalar_facts=[fact]),
        findings=[_finding("pass")],
    )

    assert report.publication_checks.grounding == "pass"
    summary = report.sections[0].theses[0]
    assert [ref.evidence_id for ref in summary.evidence_refs] == [fact.evidence_id]
    assert summary.evidence_refs[0].document_version == "doc-v2"


def test_report_publication_is_blocked_when_grounding_fails():
    narrative = NarrativeOutput(
        run_id="run-1",
        text="Неподтверждённое число 999",
        claims=[NumericClaimRecord(text="999", kind="financial", value=999.0)],
    )
    with pytest.raises(ValueError, match="publication blocked"):
        build_report_output(
            run_id="run-1", narrative=narrative, judgments=[], explanations=[],
            bundle=EvidenceBundleV2(run_id="run-1"), findings=[_finding("fail")],
        )
