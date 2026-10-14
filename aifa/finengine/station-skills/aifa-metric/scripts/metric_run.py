#!/usr/bin/env python3
"""Single natural-language station ingress for the seven Slice-27 goals.

Unlike compatibility shortcuts that hard-code form/full-analysis, this shell
does not know the GoalType in advance.  It asks the real `intent.parse` atom
to classify the literal query and routes all seven supported GoalType through
the same production runtime.  It is one ingress, not seven pipelines.
It never invokes a legacy business pipeline and never reconstructs financial
values from text or markdown.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def meta_sidecar(text_path: str) -> Path:
    return Path(text_path[: -len(".txt")] + ".meta.json" if text_path.endswith(".txt") else text_path + ".meta.json")


def _document_id(text_path: str, explicit: str) -> str:
    return explicit.strip() or Path(text_path).stem.removeprefix("ocr-")


def _case_identity(text_path: str) -> tuple[str, str]:
    owner_id = os.environ.get("FE_OWNER_ID", "").strip()
    case_id = os.environ.get("FE_CASE_ID", "").strip()
    sidecar = meta_sidecar(text_path)
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


# Manifest key -> the Contract's own `output_type` discriminant (Срез 27):
# a single lookup table, not a chain of isinstance branches per script.
_MANIFEST_KEY_OF_OUTPUT_TYPE = {
    "ExtractedMetric": "extracted_metric",
    "CalculatedMetric": "calculated_metric",
    "FormOutput": "form_output",
    "ReportOutput": "report_output",
    "PeriodChangeReport": "period_change_report",
    "FileArtifact": "file_artifact",
}


def _render_result(value) -> str:
    from finengine.contracts import (
        CalculatedMetric, ExtractedMetric, FileArtifact, FormOutput,
        PeriodChangeReport, ReportOutput,
    )

    if isinstance(value, ExtractedMetric):
        fact = value.scalar_fact
        return (
            f"{value.concept} ({value.period_ref}): {fact.value} {fact.unit} "
            f"[{value.definition_origin}, источник {fact.source_ref}]"
        )
    if isinstance(value, CalculatedMetric):
        fact = value.derived_fact
        return (
            f"{value.concept} ({value.period_ref}) = {fact.expression} = {fact.value}"
            f"{(' ' + fact.unit) if fact.unit else ''} [{value.definition_origin}, входы {len(fact.inputs)}]"
        )
    if isinstance(value, FormOutput):
        supported = sum(1 for row in value.rows if row.status == "supported")
        return f"FormOutput: {supported} из {len(value.rows)} строк подтверждены"
    if isinstance(value, ReportOutput):
        return f"ReportOutput: {len(value.sections)} разделов, статус {value.status}"
    if isinstance(value, PeriodChangeReport):
        compared = sum(1 for row in value.rows if row.status in {"computed", "zero_base"})
        return (
            f"PeriodChangeReport: {compared} из {len(value.rows)} показателей сравнены, "
            f"объяснение: {value.explanation_status}"
        )
    assert isinstance(value, FileArtifact)
    return f"FileArtifact: {value.filename} ({value.file_kind})"


def _run(args: argparse.Namespace) -> int:
    from finengine.llm import station_bridge, station_progress
    from finengine.skills.analysis import methodology
    from finengine.skills.atoms.intent import IntentParseError, parse_intent, root_from_goal
    from finengine.skills.production_runtime import (
        execute_production_goal, prepare_production_context,
    )
    from finengine.skills.result_events import typed_error_event
    from finengine.skills.station_guard import refuse_if_duplicate, start_heartbeat
    from finengine.skills.versioning import methodology_fingerprint
    from finengine.store.cases import get_case_store

    station_bridge.install_if_sealed()
    refuse_if_duplicate("aifa-metric/scripts/metric_run.py")
    heartbeat = start_heartbeat("aifa-metric")

    text = Path(args.text).read_text(encoding="utf-8")
    document_id = _document_id(args.text, args.document_id)
    owner_id, case_id = _case_identity(args.text)

    try:
        goal = asyncio.run(parse_intent(args.query))
    except IntentParseError as exc:
        event = typed_error_event(code="intent_parse_failed", message=str(exc), retryable=True)
        station_progress.emit(event)
        print(f"🔴 ОТКАЗ: {exc}", file=sys.stderr)
        return 4

    if goal.ambiguities or (not goal.targets and goal.unresolved and goal.intent_kind not in ("extract_ad_hoc_metric", "calculate_ad_hoc_metric")):
        # Явная просьба уточнить — не угадывание. Ad-hoc виды сами строят
        # корень из unresolved[0] (root_from_goal), поэтому непустой
        # unresolved для них — штатный путь, не повод отказывать здесь.
        parts = [f"«{a.term}»: {', '.join(a.candidates) or 'нет кандидатов'}" for a in goal.ambiguities]
        parts += [f"не распознано: «{u}»" for u in goal.unresolved]
        message = "нужно уточнение: " + "; ".join(parts) if parts else "запрос не удалось разобрать"
        event = typed_error_event(code="intent_ambiguous", message=message, retryable=True)
        station_progress.emit(event)
        print(f"🔴 ОТКАЗ: {message}", file=sys.stderr)
        return 4

    store = get_case_store()
    root_extra = {"owner_id": str(owner_id), "project_id": case_id}
    # `monitor_changes` (Срез 32) — те же правила, что у полного анализа: ролевой набор
    # показателей, методика в паспорте; проверок Case/снимков нет.
    analysis = goal.intent_kind in ("full_analysis", "monitor_changes")
    methodology_version = None
    if analysis:
        effective_methodology = methodology.base()
        methodology_version = str(
            methodology_fingerprint(effective_methodology).get("effective_sha256") or "unversioned"
        )
        root_extra.update({
            "depth": "full",
            "analysis_type": "general",
            "methodology_text": methodology.framework_block(
                effective_methodology, "non_financial", "full", (),
            ),
        })
    if goal.intent_kind == "form":
        root_extra["role"] = goal.role or ""
    context = prepare_production_context(
        owner_id=owner_id, case_id=case_id, document_id=document_id, document_text=text,
        goal_ir=goal, cell=args.cell, company_type_override=args.company_type or None,
        methodology_version=methodology_version, store=store,
    )
    if analysis:
        # The methodology block is profile-sensitive; replace the
        # provisional company type now that classification is frozen.
        effective_methodology = methodology.base()
        root_extra["methodology_text"] = methodology.framework_block(
            effective_methodology, context.passport.classification.company_type, "full", (),
        )

    try:
        roots = root_from_goal(goal)
    except (NotImplementedError, ValueError) as exc:
        code = "unsupported_goal_type" if isinstance(exc, NotImplementedError) else "goal_needs_clarification"
        event = typed_error_event(code=code, message=str(exc), retryable=isinstance(exc, ValueError))
        station_progress.emit(event)
        print(f"🔴 ОТКАЗ: {exc}", file=sys.stderr)
        return 4
    if len(roots) != 1:
        message = (
            f"canonical ingress ждёт один корневой результат за ход, разобралось {len(roots)}; "
            "уточните один показатель или запросите форму"
        )
        event = typed_error_event(code="multiple_targets_not_supported", message=message, retryable=True)
        station_progress.emit(event)
        print(f"🔴 ОТКАЗ: {message}", file=sys.stderr)
        return 4
    outcome = execute_production_goal(
        context, goal, document_text=text,
        root_extra=root_extra,
        event_sink=station_progress.emit,
    )
    if outcome.refusal_event is not None:
        print(json.dumps(outcome.refusal_event, ensure_ascii=False))
        return 4

    root_output_type = roots[0].root.output_type
    manifest_key = _MANIFEST_KEY_OF_OUTPUT_TYPE.get(root_output_type)
    if manifest_key is None:
        # intent.parse классифицировало запрос как что-то за пределами
        # четырёх точечных видов этого входа (например, полный анализ) —
        # честный typed-отказ с указанием, куда обратиться, не второй решатель.
        message = (
            f"intent.parse распознал неподдерживаемый этим срезом {goal.intent_kind!r} "
            f"({root_output_type})"
        )
        event = typed_error_event(code="unsupported_goal_type", message=message, retryable=False)
        station_progress.emit(event)
        print(f"🔴 ОТКАЗ: {message}", file=sys.stderr)
        return 4

    values = [
        value
        for item in outcome.roots
        for value in item.execution.evidence_by_key.get(item.planned.record.graph.root, ())
    ]
    matches = [v for v in values if getattr(v, "output_type", None) == root_output_type]
    if not matches or not outcome.succeeded:
        raise RuntimeError(f"canonical {root_output_type} was not produced")
    terminal = matches[-1]

    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "document_id": document_id,
        "document_version": context.document_version,
        "run_passport": context.passport.model_dump(mode="json"),
        manifest_key: terminal.model_dump(mode="json"),
    }
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    station_progress.emit({"type": "done", "result": manifest})
    heartbeat()
    print(_render_result(terminal))
    print("\n<<<JSON>>>")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="AIFA CLAW canonical seven-goal natural-language ingress")
    parser.add_argument("--text", required=True, help="ВНУТРЕННИЙ: delivered OCR text path")
    parser.add_argument("--document-id", default="", help="ВНУТРЕННИЙ: stable document id")
    parser.add_argument("--query", required=True, help="буквальный запрос пользователя")
    parser.add_argument("--company-type", default="", choices=["", "bank", "non_financial"])
    parser.add_argument("--cell", default="")
    parser.add_argument("--out-json", default="metric.json")
    args = parser.parse_args()
    try:
        raise SystemExit(_run(args))
    except Exception as exc:
        from finengine.llm import station_progress
        from finengine.skills.result_events import typed_error_event

        event = typed_error_event(code="canonical_metric_failed", message=str(exc), retryable=False)
        station_progress.emit(event)
        station_progress.emit({"type": "error", "message": str(exc)})
        print(f"🔴 ОТКАЗ: {exc}", file=sys.stderr)
        raise SystemExit(4) from None


if __name__ == "__main__":
    main()
