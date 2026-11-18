"""Append-only log of every extraction run.

Each completed run lands as one JSON object in
`<docs_dir>/extract_runs.jsonl`. The file is the source of truth for the
benchmark builder — `tests/build_unified_benchmark.py` (and a future
"per-arch / per-company-type" view) will read it to score recall/precision
against GT and accumulate statistics over time.

Why JSONL on disk instead of a DB:
- The runs are append-only and read in bulk for analysis — no random access
  is needed.
- Lives in the same docker volume as everything else, gets backed up by
  the daily backup.sh.
- Easy to inspect with `tail -f` and `jq`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from finengine.config import settings


_LOCK = threading.Lock()
_LOG_PATH = settings.docs_dir / "extract_runs.jsonl"
_CHAT_LOG_PATH = settings.docs_dir / "chat_runs.jsonl"
# Прогоны АНАЛИЗА (`fe_analyze`). Заведён вместе с паспортом версий (#155): у
# анализа был артефакт, но не было строки в журнале — то есть на вопрос «какие
# анализы шли по методике ДО её правки» ответить было нечем, а именно его и
# задают, когда вчерашние отчёты перестали сходиться с сегодняшними. Артефакт на
# этот вопрос не отвечает: он лежит в папке проекта пользователя, а журнал —
# один на стенд и читается насквозь.
_ANALYSIS_LOG_PATH = settings.docs_dir / "analysis_runs.jsonl"
# СЫРЬЁ ХОДА СТАНЦИИ (#249). Три журнала выше пишутся ТОЛЬКО на успехе — их
# зовёт `finalize_*`. Когда ход падает на середине, не пишется ничего, а
# комната станции с её каналом прогресса и выводом скриптов живёт ~18 минут и
# уносит улики с собой. За 15.08 это дважды стоило разбора: пока доходили руки,
# комнаты уже не было, и причина отказа шага была невосстановима.
#
# Здесь ход оседает ПОСОБЫТИЙНО, по мере поступления, независимо от того, чем
# он кончится. Отдельным файлом, а не в общий: событий на порядок больше
# записей, и смешивать поток с итогами значит сделать нечитаемыми оба.
_STATION_EVENTS_PATH = settings.docs_dir / "station_events.jsonl"
# Captured at import time so every run record carries the deploy version that
# produced it. `deploy/skill-hub/ship.sh` sets FINENGINE_GIT_VER
# (human-readable `vN` where N = commit count on HEAD) + FINENGINE_GIT_SHA
# (immutable short-sha). Local dev gets "local-dev" for both.
_GIT_SHA = os.environ.get("FINENGINE_GIT_SHA", "").strip() or "local-dev"
_GIT_VER = os.environ.get("FINENGINE_GIT_VER", "").strip() or "local-dev"


# ── Куда это едет теперь ──────────────────────────────────────────────────
# 🔴 ЖУРНАЛЫ ПРОГОНОВ ПЕРЕЕХАЛИ С ТОМА В ОБЩЕЕ ХРАНИЛИЩЕ (26.08.2026), правило
# `docs/rules/architecture.md` 3.1: «Нет состояния на файловой системе
# контейнера… Нет локальных лог-файлов». Форма у них ровно та же, что у следов
# и аудита: дописать строку, прочитать по ключу.
#
# ⚠️ СЫРЬЁ СТАНЦИИ КЛАДЁТСЯ ПО НОНСУ, а не в одну общую кучу. Прежде это был
# один файл на всё, и чтобы посмотреть один ход, его перебирали целиком
# (`grep <нонс> station_events.jsonl` — так я и делал весь день). Ключ — нонс,
# и разбор одного хода становится одним чтением.
ЖУРНАЛ_ИЗВЛЕЧЕНИЙ = "run_log_extract"
ЖУРНАЛ_ЧАТА = "run_log_chat"
ЖУРНАЛ_РАЗБОРОВ = "run_log_analysis"
# Завершения цикла: какие ссылки назвал ответ и есть ли они в `Case` (ADR-0040 п. 1 — замер для решения после MVP).
ЖУРНАЛ_ЗАВЕРШЕНИЙ = "run_log_finish"
СЫРЬЁ_СТАНЦИИ = "station_events"
# Телеметрия фронта: что видел пользователь (обрыв связи, догон ответа,
# подрезка хранилища). Пишет её `api/main`, читаем при разборах вручную.
ЖУРНАЛ_КЛИЕНТА = "client_events"
_ОБЩИЙ = "all"          # ключ журналов, у которых своего ключа нет
_ВЛАДЕЛЕЦ = "_"


def _записать(вид: str, ключ: str, запись: dict) -> None:
    """Дописать строку журнала. Журнал не вправе уронить то, ради чего пишется."""
    from finengine.store import journals  # noqa: PLC0415

    journals.append(вид, owner=_ВЛАДЕЛЕЦ, key=ключ, entry=запись)


def прочитать(вид: str, ключ: str = _ОБЩИЙ, *, файл: "Path | None" = None) -> list[dict]:
    """Строки журнала: из хранилища, иначе со старого файла на томе.

    Откат тот же, что у текста документа и у журнала прогона: спросить
    хранилище, потом диск. Без него записи, накопленные до переезда, исчезли
    бы из админских выгрузок в тот же миг, когда включили новое хранилище.
    """
    from finengine.store import journals  # noqa: PLC0415

    строки = journals.read(вид, owner=_ВЛАДЕЛЕЦ, key=ключ)
    if строки or файл is None or not файл.is_file():
        return строки
    итог: list[dict] = []
    for строка in файл.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not строка.strip():
            continue
        try:
            итог.append(json.loads(строка))
        except json.JSONDecodeError:
            continue
    return итог


def log_run(
    *,
    doc_id: str,
    filename: str,
    arch: str,
    model_id: str | None,
    company_type: str,
    company_name: str,
    period_current: str,
    period_prev: str,
    user_id: int | None,
    started_at: float,
    metrics: list[dict[str, Any]],
    error: str | None = None,
    diag: dict | None = None,
    versions: dict | None = None,
) -> None:
    """Append one record to the runs log. Best-effort: errors are swallowed
    (logging musn't break extract).

    `versions` — паспорт ПРАВИЛ прогона (#155): отпечаток реестра ячейки, по
    которому он посчитан. Отдельным полем, а не внутри `diag`: `diag` — свалка
    служебных мелочей, а версия правил равноправна `git_ver` и ищется по имени.
    Версия кода на реестр не реагирует вовсе — правка формулы деплой не двигает.
    """
    try:
        elapsed = time.time() - started_at
        found = sum(1 for m in metrics if m.get("found"))
        total = len(metrics)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_ver": _GIT_VER,
            "git_sha": _GIT_SHA,
            "doc_id": doc_id,
            "filename": filename,
            "arch": arch,
            "model_id": model_id or "",
            "company_type": company_type,
            "company_name": company_name,
            "period_current": period_current,
            "period_prev": period_prev,
            "user_id": user_id,
            "elapsed_s": round(elapsed, 1),
            "found": found,
            "total": total,
            # ⚠️ РАЗРЕЗ СТРОКИ ХРАНИМ (14.08.2026). Сюда приезжает СКЛЕЕННЫЙ
            # список `[*плоские, *сегментные, *групповые]`, а проекция роняла
            # `dimension`/`segment` — и различить их в журнале становилось
            # НЕЧЕМ. Замер по 25 прогонам стенда: 403 строки из 2916 (13%) —
            # сегментные и групповые, и в 19 прогонах из 25 они есть.
            #
            # Цена не теоретическая: `benchmarks/snap_bench.py::_events_from_run`
            # переигрывает журнал в события скорера и подставляет каждой строке
            # `dimension: ""`. То есть EBITDA одного сегмента приезжает скореру
            # как EBITDA КОМПАНИИ, рядом с настоящей. Качество считается по
            # набору, в который подмешаны разрезы.
            #
            # Здесь только СОХРАНЕНИЕ признака — оно ничего не меняет в текущих
            # числах. Починка самого переигрывания идёт отдельно и со сверкой
            # «до/после»: она сдвинет метрики бенчмарка, и сдвиг надо предъявить,
            # а не получить молча.
            # 🔴 ПЕРИОДЫ ХРАНИМ КАК ЕСТЬ (16.08.2026). Раньше проекция роняла
            # `period_values`, и период прогона восстанавливали из полей
            # `period_current`/`period_prev` — а туда `pick_report_years` кладёт
            # ГОЛЫЙ ГОД. Замер по стенду, движок против журнала:
            #
            #   Ак Барс (полугодие)     движок Q2 2025   журнал 2025
            #   Детский мир (полугодие) движок 6M 2023   журнал 2023
            #   Абрау-Дюрсо (год)       движок FY 2024   журнал 2024
            #
            # Движок период знал, журнал его забывал. `benchmarks/snap_bench.py`
            # читает журнал, восстанавливает «2025» и отдаёт скореру; тот
            # нормализует голый год в КОНЕЦ года — 31.12.2025, а эталон
            # полугодия ждёт 30.06.2025. Промах у КАЖДОЙ строки: F1=0 на
            # ак-барсе получен ровно так, и это была оценка ЖУРНАЛА, а не
            # движка.
            #
            # Одной парой «текущий/прошлый» это не чинится: у отчёта две оси
            # периода, и балансовая строка живёт датой (`31.12.2021`), а строка
            # ОПУ отрезком (`FY 2021`) — см. #205 п.(а). Поэтому период хранится
            # ПОСТРОЧНО.
            #
            # Форма — та же, что у движка (`period`/`value`), а не пары: вторая
            # форма того же понятия однажды уедет не в тот читатель. Лишние поля
            # записи (`raw_period`, `unit`, `page`, `source_text`) не храним —
            # они про один прогон, а журнал читают ради сверки с эталоном.
            # Цена замерена: +32% на самой жирной записи стенда (608 строк).
            "metrics": [
                {
                    "enum": m.get("enum"),
                    "value_current": m.get("value_current"),
                    "value_prev": m.get("value_prev"),
                    "unit": m.get("unit"),
                    "is_calculated": m.get("is_calculated", False),
                    "dimension": m.get("dimension") or "",
                    "segment": m.get("segment") or "",
                    "period_values": [
                        {"period": pv.get("period"), "value": pv.get("value")}
                        for pv in (m.get("period_values") or [])
                        if pv.get("value") is not None
                    ],
                }
                for m in metrics
            ],
            "error": error,
            "diag": diag or {},
            "versions": versions or {},
        }
        _записать(ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, _ОБЩИЙ, record)
    except Exception:
        # Never let logging break the request flow.
        pass


def log_station_events(nonce: str, events: list) -> None:
    """Сложить сырьё хода станции — ДО того, как его кто-то потребит.

    Зовётся из `api/station_progress_ingest.ingest` на каждой пачке. Молчит при
    любой беде: журнал не вправе уронить ход, ради которого пишется.
    """
    if not nonce or not events:
        return
    try:
        stamp = time.time()
        for ev in events:
            _записать(СЫРЬЁ_СТАНЦИИ, nonce, {"ts": stamp, "nonce": nonce, "event": ev})
    except Exception:
        # Never let logging break the request flow.
        pass


def station_events_path() -> Path:
    return _STATION_EVENTS_PATH


def runs_path() -> Path:
    return _LOG_PATH


def chat_runs_path() -> Path:
    return _CHAT_LOG_PATH


def log_analysis_run(
    *,
    doc_id: str,
    doc_filename: str,
    company_name: str,
    company_type: str,
    analysis_type: str,
    depth: str,
    cell: str,
    user_id: int | None,
    artifact_run_id: str,
    started_at: float,
    versions: dict | None = None,
    error: str | None = None,
) -> None:
    """Одна строка на прогон анализа — журнальная половина паспорта (#155).

    Запись НАМЕРЕННО без текста отчёта: он лежит в артефакте, а журнал нужен,
    чтобы одним проходом ответить «какие прогоны шли по такому-то отпечатку
    реестра и методики». `artifact_run_id` — мостик к полному тексту.

    Best-effort: тот же контракт, что у `log_run` — анализ уже посчитан, и
    падение журнала не должно его ронять.
    """
    try:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_ver": _GIT_VER,
            "git_sha": _GIT_SHA,
            "doc_id": doc_id,
            "doc_filename": doc_filename,
            "company_name": company_name,
            "company_type": company_type,
            "analysis_type": analysis_type,
            "depth": depth,
            "cell": cell,
            "user_id": user_id,
            "artifact_run_id": artifact_run_id,
            "elapsed_s": round(time.time() - started_at, 1),
            "versions": versions or {},
            "error": error,
        }
        _записать(ЖУРНАЛ_РАЗБОРОВ, _ОБЩИЙ, record)
    except Exception:
        pass


def log_chat_turn(
    *,
    session_id: str,
    user_id: int | None,
    username: str | None,
    doc_ids: list[str],
    message: str,
    response: str,
    started_at: float,
    arch: str = "",
    model_id: str | None = None,
    error: str | None = None,
    project_id: str = "",
) -> None:
    """Append one chat turn (user prompt + full assistant response) to
    `chat_runs.jsonl`. Used so we can audit what every user has asked the
    agent and what it answered — invaluable when testing with multiple
    users hits the wild and someone reports a wrong answer.

    Best-effort: errors are swallowed so logging never breaks the chat
    stream (same contract as `log_run`).
    """
    try:
        elapsed = time.time() - started_at
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_ver": _GIT_VER,
            "git_sha": _GIT_SHA,
            "session_id": session_id,
            "project_id": project_id,
            "user_id": user_id,
            "username": username or "",
            "doc_ids": doc_ids,
            "arch": arch,
            "model_id": model_id or "",
            "message": message,
            "response": response,
            "elapsed_s": round(elapsed, 1),
            "error": error,
        }
        _записать(ЖУРНАЛ_ЧАТА, _ОБЩИЙ, record)
    except Exception:
        pass


def log_finish_references(*, owner_id: str, case_id: str, action: str, references: dict[str, bool]) -> None:
    """Дописать строку о завершении цикла: названные `evidence_ids` и существует ли каждая в `Case`.

    Ссылки условием завершения не служат (ADR-0040 п. 1): строка нужна, чтобы после MVP посчитать, какая
    доля ответов опиралась на `Case`. Best-effort, как `log_chat_turn`: журнал не роняет цикл."""
    try:
        _записать(ЖУРНАЛ_ЗАВЕРШЕНИЙ, _ОБЩИЙ, {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_sha": _GIT_SHA,
            "owner_id": owner_id,
            "case_id": case_id,
            "action": action,
            "references": [{"evidence_id": key, "exists": found} for key, found in references.items()],
        })
    except Exception:
        pass
