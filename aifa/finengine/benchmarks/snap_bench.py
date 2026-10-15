"""«Сними бенч» для AIFA CLAW: replay сайтового прогона против golden.

Flow A из скилла finengine, адаптированный под наш стенд: юзер прогнал
извлечение на dev-сайте (finagent) → бэкенд записал прогон в
журнал прогонов (Postgres) → этот скрипт забирает предсказания, реплеит их через
benchmark_v2.py (без LLM) против golden-разметки и дописывает строку в НАШ
леджер benchmarks/AIFA_E2E_Benchmark.xlsx (та же форма, что
Finengine_E2E_Benchmark.xlsx: Прогоны/Детали/Подробности/Ошибки/По метрикам).

Скоринг-бандл self-contained в benchmarks/vendor/ (GT + скорер + finengine),
внешний чекаут не нужен. Обновляется benchmarks/sync_vendor.sh.

Примеры:
  .venv/bin/python benchmarks/snap_bench.py --list                 # dev по умолчанию
  .venv/bin/python benchmarks/snap_bench.py --env prod --list      # прогоны aifaclaw
  .venv/bin/python benchmarks/snap_bench.py --doc megafon-Q2-2025.pdf --run-type test
  .venv/bin/python benchmarks/snap_bench.py --env prod --doc last --run-type control
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Self-contained скоринг-бандл (см. benchmarks/vendor + sync_vendor.sh).
VENDOR = REPO / "benchmarks" / "vendor"
SSH = ["ssh", "-i", str(Path.home() / ".ssh" / "finengine_deploy"),
       "root@85.198.84.50"]
# 🔴 ЖУРНАЛ ПРОГОНОВ — В POSTGRES С 26.08.2026, файла extract_runs.jsonl на томе
# больше нет. Читаем той же дверью, что и бэкенд (run_logger.прочитать): она
# спрашивает хранилище, а на старых стендах откатывается на файл сама.
_ЧИТАЛКА = (
    "import json;"
    "from finengine.skills.run_logger import прочитать, ЖУРНАЛ_ИЗВЛЕЧЕНИЙ;"
    "[print(json.dumps(r, ensure_ascii=False, default=str)) "
    "for r in прочитать(ЖУРНАЛ_ИЗВЛЕЧЕНИЙ)[-{n}:]]"
)
LEDGER = REPO / "benchmarks" / "AIFA_E2E_Benchmark.xlsx"
RUNS_DIR = REPO / "benchmarks" / "runs"
# Один VPS 85.198.84.50, два контура (см. память topology-access).
ENV_CONTAINER = {"dev": "finengine-backend-1", "prod": "finengine-prod-backend-1"}


def _site_runs(container: str, last_n: int = 50) -> list[dict]:
    out = subprocess.run(
        [*SSH, f"docker exec {container} python -c '{_ЧИТАЛКА.format(n=last_n)}'"],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        sys.exit(f"не достал прогоны из {container}: {out.stderr[-300:]}")
    runs = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return runs


def _find_basket(stem: str, finengine_repo: Path) -> str | None:
    golden = finengine_repo / "tests" / "test_benchmark" / "data" / "golden"
    for basket_dir in sorted(golden.iterdir()):
        if (basket_dir / "gt" / f"{stem}.csv").is_file():
            return basket_dir.name
    return None


def _events_from_run(run: dict, path: Path) -> int:
    """Записать metrics прогона как события для --replay-events.

    ПЕРИОД БЕРЁМ СОХРАНЁННЫЙ, а восстанавливаем только у старых записей.
    С 16.08.2026 журнал хранит `period_values` построчно (см. `run_logger`), и
    это единственный способ померить промежуточный отчёт: восстановление из
    полей прогона даёт ГОЛЫЙ ГОД, скорер нормализует его в конец года, а
    эталон полугодия ждёт 30.06 — промах у каждой строки, F1=0 на ровном месте.

    Запасной путь оставлен для записей ДО этой правки: их в леджере уже
    посчитано, и терять возможность перепроверить старый прогон не за что.
    ⚠️ Он врёт на промежуточных отчётах ровно так, как описано выше, — поэтому
    считает и ГОВОРИТ об этом вызывающему (возврат `legacy`).
    """
    p_cur = run.get("period_current") or ""
    p_prev = run.get("period_prev") or ""
    n = 0
    legacy = 0
    with path.open("w", encoding="utf-8") as f:
        for m in run.get("metrics") or []:
            pvs = [
                {"period": pv["period"], "value": pv["value"]}
                for pv in (m.get("period_values") or [])
                if pv.get("period") and pv.get("value") is not None
            ]
            if not pvs:
                # ⚠️ «НЕТ ПЕРИОДА» И «НЕ НАЙДЕНО» — РАЗНЫЕ ВЕЩИ (19.08.2026).
                # Счётчик `legacy` считал сюда и ненайденные показатели: у них
                # нет значения, а значит и периода. На Россельхозбанке это дало
                # предупреждение «47 строк без периода, оценка занижена до нуля»
                # при полностью исправном свежем прогоне. Ложная тревога хуже
                # молчания: она дискредитирует верный замер, и его перепроверяют
                # вместо того, чтобы читать. Старую запись отличает НАЛИЧИЕ
                # значения без периода — только её и считаем.
                if m.get("value_current") is not None or m.get("value_prev") is not None:
                    legacy += 1
                if m.get("value_current") is not None and p_cur:
                    pvs.append({"period": p_cur, "value": m["value_current"]})
                if m.get("value_prev") is not None and p_prev:
                    pvs.append({"period": p_prev, "value": m["value_prev"]})
            if not pvs:
                continue
            f.write(json.dumps({
                "type": "metric",
                "enum": m.get("enum", ""),
                # 🔴 РАЗРЕЗ ПЕРЕДАЁМ, А НЕ СТИРАЕМ (19.08.2026).
                #
                # Здесь стояли две пустые строки, и они превращали замер в
                # ложь. Прогон по Россельхозбанку: движок отдал по
                # FEE_AND_COMMISSION_INCOME десять строк — плоскую (5835),
                # сегментный TOTAL (5835) и восемь строк разбивки по видам
                # комиссий (2209, 1287, 935 …). Размечены они правильно:
                # `dimension=product`, `segment="Комиссия по банковским
                # картам"` и так далее. Стерев разрез, обвязка отдавала
                # скореру ДЕСЯТЬ безымянных предсказаний одного показателя за
                # один период — девять из них шли в EFP как «лишние».
                #
                # Замер 19.08: 22 строки прогона имели непустой `dimension`, и
                # они дали 38 EFP из 48. Точность 63,5% была оценкой ОБВЯЗКИ,
                # а не движка. Я успел объявить это дефектом движка и был
                # неправ — дефект был здесь.
                #
                # Ключ скорера — `dataset|doc|enum|period|segment`, и с
                # непустым сегментом строка разбивки получает СВОЙ ключ вместо
                # столкновения с плоской. Про это же предупреждает шапка
                # `run_logger.log_run`: разрез в журнале хранится специально,
                # чтобы его можно было донести сюда.
                "segment": m.get("segment") or "",
                "dimension": m.get("dimension") or "",
                "unit": m.get("unit", ""),
                "is_calculated": bool(m.get("is_calculated")),
                "period_values": pvs,
            }, ensure_ascii=False) + "\n")
            n += 1
        # Метаданные прогона (benchmark_v2 #116): переносим вердикт
        # классификатора и cost/tokens оригинального прогона в replay-строки
        # леджера — иначе язык/стандарт = unknown, а cost/tokens = 0.
        diag = run.get("diag") or {}
        f.write(json.dumps({
            "type": "profile_detected",
            "company_type": run.get("company_type", ""),
            "standard": diag.get("standard") or run.get("arch", ""),
            "language": diag.get("language", ""),
            "confidence": diag.get("confidence"),
        }, ensure_ascii=False) + "\n")
        f.write(json.dumps({
            "type": "done",
            "period_current": p_cur,
            "period_prev": p_prev,
            "company_type": run.get("company_type", ""),
            "cost_usd": diag.get("cost_usd", 0),
            "tokens_in": diag.get("tokens_in", 0),
            "tokens_out": diag.get("tokens_out", 0),
            "llm_calls": diag.get("llm_calls", 0),
        }, ensure_ascii=False) + "\n")
    return n, legacy


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true",
                    help="показать последние сайтовые прогоны и выйти")
    ap.add_argument("--doc", default="",
                    help="filename прогона на сайте или 'last'")
    ap.add_argument("--env", choices=["dev", "prod"], default="dev",
                    help="откуда брать прогон: dev (finagent) или prod (aifaclaw)")
    ap.add_argument("--run-type", choices=["test", "control"], default="test")
    ap.add_argument("--run-name", default="", help="дефолт: aifa_<env>_<basket>_<stem>_<ts>")
    ap.add_argument("--vendor", type=Path, default=VENDOR,
                    help="корень скоринг-бандла (дефолт: benchmarks/vendor)")
    args = ap.parse_args()

    container = ENV_CONTAINER[args.env]
    runs = _site_runs(container)
    if args.list or not args.doc:
        print(f"последние прогоны [{args.env}: {container}]:")
        for r in runs[-10:]:
            print(f"  {r.get('ts', '')}  {r.get('filename'):<40} "
                  f"found {r.get('found')}/{r.get('total')}  "
                  f"model={r.get('model_id') or 'default'}  "
                  f"{r.get('elapsed_s', 0):.0f}с")
        if not args.doc:
            print("\nукажи --doc <filename|last>")
        return

    run = None
    if args.doc == "last":
        run = runs[-1] if runs else None
    else:
        for r in reversed(runs):
            if (r.get("filename") or "").lower() == args.doc.lower():
                run = r
                break
    if not run:
        sys.exit(f"прогон {args.doc!r} не найден в последних {len(runs)} записях "
                 f"(--list покажет доступные)")

    stem = Path(run["filename"]).stem
    basket = _find_basket(stem, args.vendor)
    if not basket:
        sys.exit(f"{stem}: нет golden-разметки ни в одной ячейке вендора — "
                 f"этот документ бенчем не покрыт (переименуй файл на сайте "
                 f"в golden-имя или добавь GT + пересинхронь вендор)")

    events = Path(f"/tmp/aifa_replay_{stem}.jsonl")
    n_ev, n_legacy = _events_from_run(run, events)
    diag = run.get("diag") or {}
    print(f"[{args.env}] прогон: {run['filename']} "
          f"(found {run.get('found')}/{run.get('total')}, "
          f"{run.get('elapsed_s', 0):.0f}с) → basket {basket}, событий {n_ev}")
    if n_legacy:
        # Молчать нельзя: на промежуточном отчёте восстановленный период даёт
        # конец года вместо даты баланса, и число внизу будет оценкой ЖУРНАЛА,
        # а не движка. Один раз мы уже приняли такой ноль за качество.
        print(f"  ⚠️  {n_legacy} строк БЕЗ сохранённого периода — прогон старше "
              f"16.08.2026. Период восстановлен из «{run.get('period_current') or '?'}»; на "
              f"промежуточном отчёте это ЗАНИЖАЕТ оценку до нуля. "
              f"Перепрогони документ, чтобы померить движок, а не журнал.")

    run_name = args.run_name or \
        f"aifa_{args.env}_{basket}_{stem}_{args.run_type}_{time.strftime('%Y%m%d-%H%M')}"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,  # наш .venv питон (у вендора своего venv нет)
        "tests/test_benchmark/benchmark_v2.py", "--mode", "run",
        "--run-name", run_name, "--run-type", args.run_type,
        "--suite", "golden", "--golden-basket", basket, "--pdf", f"{stem}.pdf",
        "--replay-events", str(events),
        "--runs-dir", str(RUNS_DIR), "--out-xlsx", str(LEDGER),
    ]
    if run.get("model_id"):
        cmd += ["--model-id", str(run["model_id"])]
    env = {"PYTHONPATH": "src:tests:.", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    proc = subprocess.run(cmd, cwd=args.vendor, env=env,
                          capture_output=True, text=True, timeout=900)

    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
    print(tail)
    if proc.returncode != 0:
        sys.exit(f"benchmark_v2 упал (код {proc.returncode})")

    print(f"\nтелеметрия сайтового прогона: elapsed={run.get('elapsed_s')}с, "
          f"llm_stream={diag.get('llm_stream_s')}с, "
          f"first_token={diag.get('first_token_s')}с, "
          f"tokens in/out={diag.get('tokens_in')}/{diag.get('tokens_out')}, "
          f"llm_calls={diag.get('llm_calls')}, cost=${diag.get('cost_usd')}")
    print(f"леджер: {LEDGER}")


if __name__ == "__main__":
    main()
