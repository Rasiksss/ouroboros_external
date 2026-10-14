"""A/B извлечения: один документ, две схемы, один скорер (#257).

ЗАЧЕМ. Двухстадийная схема Finengine (`finengine_2`, боевая у них с v634) —
кандидат заменить наш полноконтекстный путь. Сравнивать их по прогонам с сайта
нельзя: там разные документы, разное время и разный состав реестра. Этот скрипт
гоняет ОБА движка на ОДНОМ тексте с ОДНИМИ аргументами и пишет события в том же
виде, который понимает `benchmark_v2.py --replay-events`, — дальше обе схемы
судит один и тот же скорер по одному и тому же эталону.

ЧТО ОБЯЗАТЕЛЬНО ОДИНАКОВО У ОБЕИХ СХЕМ, иначе замер отвечает не на тот вопрос:
  * реестр ЯЧЕЙКИ (`registry_override`) — иначе сравниваем реестры, а не схемы;
  * модель — обе стадии двухстадийной прописаны в ней самой, у нашего пути
    берётся дефолт конфига; расхождение назовёт `--report`;
  * текст документа — читается один файл, никакого повторного OCR.

ГОНЯЕТСЯ НА СТЕНДЕ, внутри контейнера бэкенда: локально нет доступа к
модельному прокси (он слушает 172.17.0.1:8820 на VPS).

    docker exec finengine-backend-1 sh -c "cd /app && PYTHONPATH=src \\
      python benchmarks/ab_extract.py --text data/documents/<id>.txt \\
      --cell ifrs_bank_ru --engine current --out /tmp/ev_current.jsonl"

Потом то же с `--engine two_stage`, и оба файла — в скорер.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path


def _replay_line(ev: dict) -> dict:
    """Событие движка → строка для `--replay-events`.

    Поля берём ровно те, что скорер читает (см. snap_bench._events_from_run).
    `period_values` НЕ восстанавливаем из value_current/value_prev: на
    промежуточном отчёте это дало бы голый год, а эталон полугодия ждёт
    30.06 — промах у каждой балансовой строки (#205, замер: 100% против 29%).
    """
    return {
        "type": "metric",
        "enum": ev.get("enum", ""),
        "segment": ev.get("segment", "") or "",
        "dimension": ev.get("dimension", "") or "",
        "unit": ev.get("unit", "") or "",
        "is_calculated": bool(ev.get("is_calculated")),
        "period_values": [
            {"period": pv.get("period"), "value": pv.get("value")}
            for pv in (ev.get("period_values") or [])
            if pv.get("period") and pv.get("value") is not None
        ],
    }


async def _run(engine: str, text: str, cell: str, company_type: str,
               role: str, out: Path) -> dict:
    import uuid

    from finengine.config import settings
    from finengine.skills.extract_result import run_stages
    from finengine.skills.extract_text import _StubDoc, _StubIndex
    from finengine.skills.metric_schema import registry_for_profile

    registry = registry_for_profile(cell)
    # 🔴 ТЕМП ВТОРОЙ СТАДИИ ЗАДАЁТСЯ ЗДЕСЬ ЖЕ, ЧТО И НА БОЕВОМ ПУТИ (20.08.2026).
    #
    # Замерщик зовёт движок НАПРЯМУЮ, а параметры батчей боевому пути передаёт
    # `extract_text.extract_stream_from_text` — то есть звено, которое замерщик
    # обходит. Первый замер после включения шардирования это и показал: на
    # стенде настройки стояли (размер 10, потоков 4), а в разрезе стадий стоял
    # одинокий `main_extract_1` — замерщик померил СТАРОЕ поведение и был бы
    # прочитан как «шарды не дали выигрыша».
    #
    # ⚠️ ПРИЗНАК ТОТ ЖЕ, ИМЯ ДРУГОЕ (21.08.2026). С этого дня имя стадии несёт
    # номер попытки, и небатчевая стадия зовётся `main_extract_1_attempt_01`, а
    # батчевая — `main_extract_batch_NNN_attempt_MM`. Диагноз читается так же:
    # одинокая небатчевая стадия вместо восьми батчевых = замерщик снова обошёл
    # боевые настройки.
    #
    # Держим список ключей одним местом с `extract_text.py`: разъедутся — и
    # замер снова будет мерить не то, что работает у пользователя.
    extra: dict = {}
    if engine == "two_stage":
        from finengine.skills.two_stage_extractor import (
            extract_stream_finengine_dev_two_stage as run,
        )
        extra = {
            "extraction_batch_size": settings.extract_batch_size,
            "extraction_batch_jobs": settings.extract_batch_jobs,
            "extraction_batch_attempts": settings.extract_batch_attempts,
            "extraction_batch_strategy": settings.extract_batch_strategy,
            "extraction_ask_without_evidence": settings.extract_ask_without_evidence,
            "segment_extraction_batch_size": settings.extract_segment_batch_size,
        }
    else:
        from finengine.skills.bulk_extractor import extract_stream_finengine_dev as run

    docs_dir = Path(settings.docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    tmp_id = f"ab-{engine}-{uuid.uuid4().hex[:8]}"
    (docs_dir / f"{tmp_id}.txt").write_text(text, encoding="utf-8")
    stub = _StubIndex(tmp_id, _StubDoc(company_name="", filename=tmp_id))

    started = time.time()
    metrics, done_ev, err = [], {}, ""
    async for ev in run(
        tmp_id,
        company_type or "unknown",
        index=stub,
        role=role,
        include_segments=True,
        include_consolidated_group=True,
        registry_override=registry,
        **extra,
    ):
        kind = ev.get("type")
        if kind == "metric":
            metrics.append(ev)
        elif kind == "done":
            done_ev = ev
        elif kind == "error":
            err = str(ev.get("message", ""))[:200]
    elapsed = time.time() - started
    (docs_dir / f"{tmp_id}.txt").unlink(missing_ok=True)

    with out.open("w", encoding="utf-8") as f:
        written = 0
        for ev in metrics:
            line = _replay_line(ev)
            if not line["period_values"]:
                continue
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            written += 1
        f.write(json.dumps({
            "type": "done",
            "period_current": done_ev.get("period_current", ""),
            "period_prev": done_ev.get("period_prev", ""),
            "company_type": done_ev.get("company_type", company_type),
        }, ensure_ascii=False) + "\n")

    return {
        "engine": engine, "elapsed_s": round(elapsed, 1),
        "metric_events": len(metrics), "written": written,
        "with_segment": sum(1 for m in metrics if m.get("segment")),
        "period_current": done_ev.get("period_current", ""),
        # ПОСТАДИЙНЫЙ РАЗРЕЗ — ЕДИНСТВЕННОЕ, ЧЕМ ЭТОТ СКРИПТ МЕРИТ ВРЕМЯ (Б7).
        # `elapsed_s` выше — это время ВСЕГО прогона: по нему нельзя отличить
        # экономию на префилле (короче промпт) от экономии на генерации (меньше
        # просим) — а решения по ускорению принимаются ровно между этими двумя.
        # Движок разрез уже посчитал и положил в `done['_diag']`; до 19.08 этот
        # скрипт выбрасывал терминальное событие целиком, кроме трёх полей.
        "stages": run_stages(done_ev.get("_diag")),
        "error": err,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="файл с текстом документа")
    ap.add_argument("--cell", required=True, help="профиль ячейки, напр. ifrs_bank_ru")
    ap.add_argument("--company-type", default="", help="bank | non_financial; пусто = детектор")
    ap.add_argument("--role", default="other")
    ap.add_argument("--engine", choices=("current", "two_stage"), required=True)
    ap.add_argument("--out", required=True, help="куда положить события для скорера")
    a = ap.parse_args()

    text = Path(a.text).read_text(encoding="utf-8")
    res = asyncio.run(_run(a.engine, text, a.cell, a.company_type, a.role, Path(a.out)))
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
