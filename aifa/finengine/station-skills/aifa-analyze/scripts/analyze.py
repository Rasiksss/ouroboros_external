#!/usr/bin/env python3
"""Thin station shell for the canonical Slice-13.5 production runtime.

The shell performs transport/CLI duties only. Financial extraction,
calculation, analysis and publication are owned by CaseRuntime's PlanGraph.
Raw OCR is supplied to document-reading atoms; it is never passed to a legacy
final-analysis handler.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _document_id(text_path: str, docs: str) -> str:
    named = [value.strip() for value in docs.split(",") if value.strip()]
    if len(named) > 1:
        raise ValueError(
            f"документов {len(named)}, а один production run принимает один frozen document scope"
        )
    return named[0] if named else Path(text_path).stem.removeprefix("ocr-")


def _case_identity(text_path: str) -> tuple[str, str]:
    owner_id = os.environ.get("FE_OWNER_ID", "").strip()
    case_id = os.environ.get("FE_CASE_ID", "").strip()
    raw = str(Path(text_path))
    sidecar = Path(raw[: -len(".txt")] + ".meta.json" if raw.endswith(".txt") else raw + ".meta.json")
    if sidecar.is_file():
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        owner_id = owner_id or str(meta.get("owner_id") or "").strip()
        case_id = case_id or str(meta.get("case_id") or "").strip()
    if not owner_id or not case_id:
        raise RuntimeError(
            "station ingress requires delivered owner_id and case_id; deriving durable Case "
            "identity from nonce/text filename is forbidden"
        )
    return owner_id, case_id


def _external_executor(rows: list[dict]):
    from finengine.contracts import ExternalFactRef
    from finengine.skills.analysis import external as ext

    accepted = ext.from_rows(rows)

    def execute(args, ctx):
        passport = ctx.run_passport
        if passport is None:
            raise ValueError("explain.external requires frozen RunPassport")
        jurisdiction = ext.jurisdiction_from_standard(passport.classification.standard)
        if not jurisdiction:
            return []
        return [
            ExternalFactRef(
                text=fact.text,
                entity_id=passport.entity.entity_id,
                jurisdiction=jurisdiction,
                date=fact.date,
                source=fact.source,
                url=fact.url,
                period_ref=args.period_ref or None,
            )
            for fact in accepted
        ]

    return execute


def _render_report(report) -> str:
    lines: list[str] = []
    for section in report.sections:
        lines.append(f"## {section.title}")
        lines.extend(thesis.text for thesis in section.theses)
        lines.append("")
    return "\n".join(lines).strip()


def _run(args: argparse.Namespace) -> int:
    from finengine.contracts import GoalIR, ReportOutput
    from finengine.llm import station_bridge, station_progress
    from finengine.skills.analysis import methodology
    from finengine.skills.atoms.intent import root_from_goal
    from finengine.skills.production_runtime import execute_production_goal, prepare_production_context
    from finengine.skills.station_guard import refuse_if_duplicate, start_heartbeat
    from finengine.skills.versioning import methodology_fingerprint

    station_bridge.install_if_sealed()
    refuse_if_duplicate("aifa-analyze/scripts/analyze.py")
    start_heartbeat("aifa-analyze")

    text = Path(args.text).read_text(encoding="utf-8")
    if not Path(args.metrics).is_file():
        raise ValueError(f"canonical extraction manifest is missing: {args.metrics}")
    document_id = _document_id(args.text, args.docs)
    owner_id, case_id = _case_identity(args.text)

    deltas = []
    if args.methodology:
        deltas = (json.loads(Path(args.methodology).read_text(encoding="utf-8")) or {}).get("deltas") or []
    effective_methodology = methodology.apply(deltas, strict=False) if deltas else methodology.base()
    methodology_fp = methodology_fingerprint(effective_methodology)
    methodology_version = str(methodology_fp.get("effective_sha256") or "unversioned")

    external_rows: list[dict] = []
    if args.external:
        external_rows = (json.loads(Path(args.external).read_text(encoding="utf-8")) or {}).get("external") or []

    goal = GoalIR(
        raw_query="Полный финансовый анализ",
        intent_kind="full_analysis",
        role="other",
        parameters={"depth": args.depth, "analysis_type": args.analysis_type},
    )
    context = prepare_production_context(
        owner_id=owner_id,
        case_id=case_id,
        document_id=document_id,
        document_text=text,
        goal_ir=goal,
        cell=args.cell,
        company_type_override=args.company_type or None,
        methodology_version=methodology_version,
        registry_options={"external_executor": _external_executor(external_rows)},
    )
    roots = root_from_goal(goal)
    if len(roots) != 1 or roots[0].root.output_type != "ReportOutput":
        raise RuntimeError("FULL_FINANCIAL_ANALYSIS must resolve to one ReportOutput root")
    methodology_text = methodology.framework_block(
        effective_methodology,
        context.passport.classification.company_type,
        args.depth,
        tuple(value.strip() for value in args.lens.split(",") if value.strip()),
    )
    outcome = execute_production_goal(
        context,
        goal,
        document_text=text,
        root_extra={
            "depth": args.depth,
            "methodology_text": methodology_text,
            "analysis_type": args.analysis_type,
        },
        event_sink=station_progress.emit,
    )
    if outcome.refusal_event is not None:
        print(json.dumps(outcome.refusal_event, ensure_ascii=False))
        return 4
    reports = [
        value
        for item in outcome.roots
        for values in item.execution.evidence_by_key.values()
        for value in values
        if isinstance(value, ReportOutput)
    ]
    if not reports or not outcome.succeeded:
        raise RuntimeError("canonical ReportOutput was not produced")
    report = reports[-1]
    station_progress.emit({
        "type": "done",
        "result": {
            "schema_version": "1.0",
            "case_id": case_id,
            "document_id": document_id,
            "document_version": context.document_version,
            "run_passport": context.passport.model_dump(mode="json"),
            "report_output": report.model_dump(mode="json"),
        },
    })
    print(_render_report(report))
    print("\n<<<JSON>>>")
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="AIFA CLAW canonical financial analysis")
    parser.add_argument("--text", required=True)
    parser.add_argument("--depth", default="full", choices=["full", "quick"])
    parser.add_argument("--analysis-type", default="general")
    parser.add_argument("--company-type", default="", choices=["", "bank", "non_financial"])
    parser.add_argument("--methodology", default="", help="ВНУТРЕННИЙ: delivered frozen methodology deltas")
    parser.add_argument("--metrics", required=True, help="canonical extraction manifest; never parsed as numbers")
    parser.add_argument("--external", default="", help="ВНУТРЕННИЙ: delivered external evidence rows")
    parser.add_argument("--cell", default="")
    parser.add_argument("--lens", default="")
    parser.add_argument("--docs", default="")
    args = parser.parse_args()
    from finengine.skills.analysis import get_skill, menu_text

    analysis_skill = get_skill(args.analysis_type)
    if analysis_skill is None:
        print(
            f"🔴 ОТКАЗ: неизвестный вид анализа {args.analysis_type!r}.\n{menu_text()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if analysis_skill.status != "ready":
        print(
            f"🔴 ОТКАЗ: вид анализа {analysis_skill.title!r} ещё не готов; "
            f"нужен вход: {analysis_skill.doc_semantics}.",
            file=sys.stderr,
        )
        raise SystemExit(3)
    try:
        raise SystemExit(_run(args))
    except Exception as exc:
        from finengine.llm import station_progress
        from finengine.skills.result_events import typed_error_event

        event = typed_error_event(code="canonical_analysis_failed", message=str(exc), retryable=False)
        station_progress.emit(event)
        station_progress.emit({"type": "error", "message": str(exc)})
        print(f"🔴 ОТКАЗ: {exc}", file=sys.stderr)
        raise SystemExit(4) from None


if __name__ == "__main__":
    main()
