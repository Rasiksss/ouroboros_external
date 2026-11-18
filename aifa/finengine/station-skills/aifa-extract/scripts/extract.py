#!/usr/bin/env python3
"""Thin station shell for canonical CaseRuntime extraction.

The shell only parses transport arguments and renders existing typed results.
It never invokes the legacy two-stage business pipeline directly and never
reconstructs financial values from text or markdown.
"""
from __future__ import annotations

import argparse
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


def _local_writer(path: str):
    destination = Path(path)

    def write(_filename: str, content: bytes, _mime_type: str) -> str:
        destination.write_bytes(content)
        return str(destination.resolve())

    return write


def _render_form(form) -> str:
    lines = ["| Показатель | Статус | Значение |", "|---|---|---:|"]
    for row in form.rows:
        value = row.display_value if row.display_value is not None else "—"
        lines.append(f"| {row.label} | {row.status} | {value} |")
    return "\n".join(lines)


def _summary_line(form) -> str:
    """Итог читается ОДНИМ способом — числом найденных ЗНАЧЕНИЙ, до таблицы.

    Печатается ДО таблицы, потому что модель (суб-агент, читающий stdout
    `exec`) читает вывод сверху вниз: поставь таблицу первой, и она снова
    посчитает извлечённым число СТРОК, а не число найденных значений
    (живой дефект НОВАТЭК 31.08.2026: 6 из 122 прочитано как «122 извлечено»).
    """
    total = len(form.rows)
    found = sum(1 for row in form.rows if row.status == "supported")
    line = f"Извлечено ЗНАЧЕНИЙ: {found} из {total} запрошенных показателей."
    if total and found / total < 0.5:
        line += (
            "\n⚠️ ПРЯМО скажи пользователю: найдено меньше половины "
            "запрошенного."
        )
    return line


def _role_notice_line(form, passport) -> str:
    """Роль не заведена для этой пары (роль × отчётность) — сказать вслух.

    Суб-агент читает ЭТОТ stdout напрямую (`exec`'s return), не только то,
    что позже соберёт слушатель станции (`station_turn.py`) для истории хода
    или для чата уже прикреплённой комнаты. Без этой строки на паре «ДГР ×
    нефинансовая» суб-агент увидел бы 119 строк раскладки-заглушки и не узнал
    бы, что выбор роли не сработал (найдено 20.08.2026).
    """
    from finengine.roles import scope as _scope

    role = str(form.role or "")
    if not role:
        return ""
    classification = passport.classification
    plan = _scope.build_role_plan(role, classification.company_type, classification.profile_id)
    if not _scope.role_not_applied(plan):
        return ""
    return _scope.role_notice(plan, classification.company_type, classification.profile_id)


def _run(args: argparse.Namespace) -> int:
    from finengine.contracts import FileArtifact, FormOutput, GoalIR
    from finengine.llm import station_bridge, station_progress
    from finengine.skills.extract_result import form_refusal_reason
    from finengine.skills.production_runtime import execute_production_goal, prepare_production_context
    from finengine.skills.result_events import typed_error_event, typed_result_event
    from finengine.skills.station_guard import refuse_if_duplicate, start_heartbeat

    station_bridge.install_if_sealed()
    refuse_if_duplicate("aifa-extract/scripts/extract.py")
    heartbeat = start_heartbeat("aifa-extract")

    if args.extra_metrics or args.extra_calculated:
        raise NotImplementedError(
            "station ad-hoc definition files are not a canonical Case definition source; "
            "use EXTRACT_AD_HOC_METRIC/CALCULATE_AD_HOC_METRIC ingress"
        )
    text = Path(args.text).read_text(encoding="utf-8")
    document_id = _document_id(args.text, args.document_id)
    owner_id, case_id = _case_identity(args.text)

    requested = tuple(value.strip() for value in args.metrics.split(",") if value.strip())
    parameters = {}
    registry_options = {}
    if args.out_xlsx:
        parameters = {"file_kind": "xlsx", "template_version": "1"}
        registry_options["file_writer"] = _local_writer(args.out_xlsx)
    goal = GoalIR(
        raw_query="Извлечь показатели в типизированную форму",
        intent_kind="form",
        role=args.role,
        parameters=parameters,
    )
    context = prepare_production_context(
        owner_id=owner_id,
        case_id=case_id,
        document_id=document_id,
        document_text=text,
        goal_ir=goal,
        cell=args.cell,
        company_type_override=args.company_type or None,
        registry_options=registry_options,
    )
    root_extra = {
        "role": args.role,
        "requested_concepts": ",".join(requested),
        "owner_id": str(owner_id),
        "project_id": case_id,
    }
    if args.out_xlsx:
        root_extra["filename"] = Path(args.out_xlsx).name
    outcome = execute_production_goal(
        context,
        goal,
        document_text=text,
        root_extra=root_extra,
        event_sink=station_progress.emit,
    )
    if outcome.refusal_event is not None:
        print(json.dumps(outcome.refusal_event, ensure_ascii=False))
        return 4

    values = [
        value
        for item in outcome.roots
        for payloads in item.execution.evidence_by_key.values()
        for value in payloads
    ]
    forms = [value for value in values if isinstance(value, FormOutput)]
    files = [value for value in values if isinstance(value, FileArtifact)]
    if not forms or not outcome.succeeded:
        raise RuntimeError("canonical FormOutput was not produced")
    form = forms[-1]
    # Безусловный отказ (Срез 13.5.4): разобрано НОЛЬ значений из запрошенных.
    # Тот же порог, что уже решён владельцем для `refusal_reason` двухстадийного
    # движка — станция спрашивает ЯДРО и выходит ненулевым кодом, а не решает
    # сама (`extract_result.form_refusal_reason`).
    refusal = form_refusal_reason(form.model_dump(mode="json"))
    if refusal:
        event = typed_error_event(code="empty_extraction", message=refusal, retryable=False)
        station_progress.emit(event)
        station_progress.emit({"type": "error", "message": refusal})
        print(f"🔴 ОТКАЗ: {refusal}", file=sys.stderr)
        return 4
    # When FileArtifact is the graph root, FormOutput is an internal typed
    # dependency; publish it explicitly so UI and file are driven by one value.
    if files:
        station_progress.emit(typed_result_event(form))

    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "document_id": document_id,
        "document_version": context.document_version,
        "run_passport": context.passport.model_dump(mode="json"),
        "form_output": form.model_dump(mode="json"),
        "file_artifact": files[-1].model_dump(mode="json") if files else None,
    }
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    station_progress.emit({"type": "done", "result": manifest})
    heartbeat()
    print(_summary_line(form))
    role_notice = _role_notice_line(form, context.passport)
    if role_notice:
        print(f"⚠️ {role_notice}")
    print(_render_form(form))
    print("\n<<<JSON>>>")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="AIFA CLAW canonical typed extraction")
    parser.add_argument("--text", required=True, help="ВНУТРЕННИЙ: delivered OCR text path")
    parser.add_argument("--document-id", default="", help="ВНУТРЕННИЙ: stable document id")
    parser.add_argument("--role", default="other")
    parser.add_argument("--company-type", default="", choices=["", "bank", "non_financial"])
    parser.add_argument("--out-xlsx", default="")
    parser.add_argument("--basis-full", action="store_true")
    parser.add_argument("--metrics", default="")
    parser.add_argument("--segments", default="auto", choices=["auto", "on", "off"])
    parser.add_argument("--cell", default="")
    parser.add_argument("--extra-calculated", default="")
    parser.add_argument("--extra-metrics", default="")
    parser.add_argument("--out-json", default="metrics.json")
    args = parser.parse_args()
    try:
        raise SystemExit(_run(args))
    except Exception as exc:
        from finengine.llm import station_progress
        from finengine.skills.result_events import typed_error_event

        code = "unsupported_goal_type" if isinstance(exc, NotImplementedError) else "canonical_extraction_failed"
        event = typed_error_event(code=code, message=str(exc), retryable=False)
        station_progress.emit(event)
        station_progress.emit({"type": "error", "message": str(exc)})
        print(f"🔴 ОТКАЗ: {exc}", file=sys.stderr)
        raise SystemExit(4) from None


if __name__ == "__main__":
    main()
