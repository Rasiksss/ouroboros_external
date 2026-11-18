"""``out.report``: a structured publication over verified typed analysis."""
from __future__ import annotations

from collections import defaultdict

from finengine.contracts import (
    EvidenceBundleV2,
    EvidenceRef,
    Explanation,
    Judgment,
    NarrativeOutput,
    OutputGap,
    OutputRunMetadata,
    OutputWarning,
    PublicationChecks,
    ReportOutput,
    ReportSection,
    ReportThesis,
    VerificationFinding,
)

ATOM_VERSION = "1.0.0"


def _bundle_refs(bundle: EvidenceBundleV2 | None) -> dict[str, EvidenceRef]:
    if bundle is None:
        return {}
    refs: dict[str, EvidenceRef] = {}
    for fact in (*bundle.scalar_facts, *bundle.derived_facts):
        refs[fact.evidence_id] = EvidenceRef(
            evidence_id=fact.evidence_id,
            concept=fact.concept,
            period_ref=fact.period_ref,
            source_ref=fact.source_ref,
            document_version=fact.document_version,
        )
    return refs


def _gate_status(findings: list[VerificationFinding], kind: str) -> str:
    relevant = [finding for finding in findings if finding.check_kind == kind]
    if not relevant:
        return "not_applicable"
    return "fail" if any(finding.status == "fail" for finding in relevant) else "pass"


def build_report_output(
    *, run_id: str, narrative: NarrativeOutput, judgments: list[Judgment],
    explanations: list[Explanation], bundle: EvidenceBundleV2 | None,
    findings: list[VerificationFinding], passport=None,
) -> ReportOutput:
    grounding = _gate_status(findings, "grounding")
    citation = _gate_status(findings, "citation")
    if narrative.claims and grounding != "pass":
        raise ValueError("out.report: publication blocked because grounding gate did not pass")
    if citation == "fail":
        raise ValueError("out.report: publication blocked because citation gate failed")

    evidence = _bundle_refs(bundle)
    explanation_by_judgment: dict[str, list[Explanation]] = defaultdict(list)
    for explanation in explanations:
        explanation_by_judgment[explanation.judgment_id].append(explanation)

    sections: list[ReportSection] = []
    covered_claims: set[int] = set()
    for judgment in judgments:
        thesis_refs = [evidence[eid] for eid in judgment.supporting_evidence_ids if eid in evidence]
        theses = [ReportThesis(
            text=judgment.verdict,
            status="supported" if thesis_refs else "missing",
            evidence_refs=thesis_refs,
        )]
        for explanation in explanation_by_judgment.get(judgment.judgment_id, []):
            refs = [evidence[eid] for eid in explanation.cited_evidence_ids if eid in evidence]
            claims = []
            cited_ids = set(explanation.cited_evidence_ids)
            for index, claim in enumerate(narrative.claims):
                if claim.evidence_id and claim.evidence_id in cited_ids:
                    claims.append(claim)
                    covered_claims.add(index)
            theses.append(ReportThesis(
                text=explanation.text,
                status="supported" if refs or not claims else "missing",
                evidence_refs=refs,
                claims=claims,
            ))
        sections.append(ReportSection(
            section_id=f"concept:{judgment.concept}",
            title=judgment.concept,
            status="supported" if thesis_refs else "missing",
            theses=theses,
        ))

    uncategorized = [claim for index, claim in enumerate(narrative.claims) if index not in covered_claims]
    if narrative.text:
        refs = [
            evidence[eid] for eid in narrative.cited_evidence_ids
            if eid in evidence
        ]
        sections.insert(0, ReportSection(
            section_id="summary",
            title="Итог",
            status="supported" if refs or not uncategorized else "missing",
            theses=[ReportThesis(
                text=narrative.text,
                status="supported" if refs or not uncategorized else "missing",
                evidence_refs=refs,
                claims=uncategorized,
            )],
        ))

    gaps = list(narrative.gaps)
    for section in sections:
        if section.status == "missing":
            gaps.append(OutputGap(
                code="report_section_missing_evidence",
                concept=section.section_id.removeprefix("concept:"),
                message=f"Раздел «{section.title}» не имеет разрешаемой ссылки на доказательство",
            ))
    warnings = list(narrative.warnings)
    warnings.extend(
        OutputWarning(code=f"verification_{finding.check_kind}", message=finding.detail or finding.status)
        for finding in findings if finding.status != "pass"
    )
    return ReportOutput(
        run_id=run_id,
        status="partial" if gaps else "complete",
        sections=sections,
        gaps=gaps,
        warnings=warnings,
        publication_checks=PublicationChecks(grounding=grounding, citation=citation),
        run_metadata=OutputRunMetadata.from_passport(run_id, passport),
    )


def out_report_executor(args, ctx) -> list[ReportOutput]:
    deps = ctx.dependencies()
    narrative = next((item for item in deps if isinstance(item, NarrativeOutput)), None)
    judgments = [item for item in deps if isinstance(item, Judgment)]
    explanations = [item for item in deps if isinstance(item, Explanation)]
    bundle = next((item for item in deps if isinstance(item, EvidenceBundleV2)), None)
    if narrative is None:
        has_evidence = judgments or explanations or (bundle and (bundle.scalar_facts or bundle.derived_facts))
        if has_evidence:
            raise ValueError("out.report: verified NarrativeOutput is required")
        # Нечего излагать: документ не дал ни одного факта. Это «данных нет» — пустой результат, как у
        # `out.narrative` без суждений, а не сбой узла (граница части 14: сбой узла уводит задачу в цикл).
        return []
    return [build_report_output(
        run_id=ctx.run_id,
        narrative=narrative,
        judgments=judgments,
        explanations=explanations,
        bundle=bundle,
        findings=[item for item in deps if isinstance(item, VerificationFinding)],
        passport=ctx.run_passport,
    )]


__all__ = ["ATOM_VERSION", "build_report_output", "out_report_executor"]
