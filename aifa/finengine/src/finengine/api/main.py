"""FastAPI application for Finengine."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

from contextlib import ExitStack
from starlette.background import BackgroundTask
from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

# App-INFO наружу: у root-логгера нет хендлера (uvicorn настраивает только свои),
# из-за чего INFO приложения (bg mailbox, ingest, драйвер станции) молча глотался
# и наблюдаемость фоновых путей была слепой. WARNING+ печатался lastResort-ом.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from finengine.auth.bootstrap import bootstrap_admin  # noqa: E402
from finengine.auth.db import User  # noqa: E402
from finengine.auth.dependencies import get_current_user  # noqa: E402
from finengine.auth.routes import router as auth_router  # noqa: E402
from finengine.demo.routes import router as demo_router  # noqa: E402
from finengine.config import settings  # noqa: E402
from finengine.ingestion.pdf_parser import parse_pdf  # noqa: E402
from finengine.ingestion.document_store import get_store  # noqa: E402
from finengine.models.document import DocumentMeta, DocType, CompanyType  # noqa: E402
from finengine.models.query import QueryRequest, QueryResponse  # noqa: E402
from finengine.store.dialogs import get_dialog_store  # noqa: E402
from finengine.orchestrator.agent import get_agent  # noqa: E402

# Sentry — must be initialized before the FastAPI app is created so the
# integration captures startup errors too. Empty DSN = SDK is a no-op.
if settings.sentry_dsn_backend:
    import sentry_sdk
    from sentry_sdk.integrations.starlette import StarletteIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration

    def _before_send(event, hint):
        # Strip the OpenRouter / Anthropic auth headers and proxy URL out of
        # exception payloads. The SDK already redacts cookies / Authorization,
        # but our HTTPS_PROXY / OPENROUTER_API_KEY may end up in repr() of
        # client errors.
        try:
            if "exception" in event:
                for exc in event["exception"].get("values", []):
                    val = exc.get("value", "")
                    if "openrouter.ai" in val or "anthropic.com" in val:
                        exc["value"] = "(redacted — contained provider URL)"
        except Exception:
            pass
        return event

    sentry_sdk.init(
        dsn=settings.sentry_dsn_backend,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        send_default_pii=False,
        before_send=_before_send,
    )

app = FastAPI(
    title="Finengine API",
    description="Financial IFRS Analysis Agent",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔴 РЕЕСТР БЕРЁТСЯ ВЫЗОВОМ, А НЕ МОДУЛЬНОЙ ПЕРЕМЕННОЙ (26.08.2026).
# Здесь стояло `index = get_store()` — имя от эпохи FAISS и обращение к базе
# НА ИМПОРТЕ модуля. Второе хуже: импорт `api.main` начинал зависеть от того,
# поднят ли Postgres, и любой тест, который просто импортирует роуты, требовал
# бы живой базы. `get_store()` кэширует экземпляр — вызывать её дёшево, а общий
# экземпляр с агентом и fe_*-тулами сохраняется (ради него переменная и
# заводилась: свой экземпляр у API делал загруженный документ невидимым агенту).

# Загрузки идут по одной. Пикла с его небезопасной записью больше нет, но замок
# оставлен: демо-роут грузит документ и файл вместе, и параллельные загрузки
# одного слага дублировали бы объект в хранилище.
_upload_lock = asyncio.Lock()


app.include_router(auth_router)
app.include_router(demo_router)

# Personal sources + per-chat knowledge base (feature/personal-sources-kb).
# Mounts /sources/* and /chats/<id>/kb/* — the frontend's Sources Panel
# and the chat composer's URL drop-zone both call these.
from finengine.api.personal_sources import router as _personal_router  # noqa: E402
app.include_router(_personal_router)

# Scenarios («Сценарии») — user-defined scheduled tasks. Mounts /scenarios/*.
from finengine.api.scenarios import router as _scenarios_router  # noqa: E402
app.include_router(_scenarios_router)

# Station progress ingest (#83) — daemon relays sealed-station progress here,
# routed by nonce to the right SSE. Additive: own /internal/* route, own auth.
from finengine.api.station_progress_ingest import router as _station_progress_router  # noqa: E402
app.include_router(_station_progress_router)

# #84 Фаза C узел №1: внутренняя выдача OCR-текста гейтвею-плагину (доставка
# документа в комнату воркера через files:upload). Аддитивно, тот же токен.
from finengine.api.internal_docs import router as _internal_docs_router  # noqa: E402
app.include_router(_internal_docs_router)
# #123: личная методика пользователя — тем же каналом и тем же токеном, что и
# текст документа. Без этого роута правка пользователя не доезжает до разбора.
from finengine.api.internal_methodology import (  # noqa: E402
    router as _internal_methodology_router)
app.include_router(_internal_methodology_router)
# #98 C3-2: исполнение fe-тулов для плагина гейтвея (петля у гейтвея).
from finengine.api.internal_tools import router as _internal_tools_router  # noqa: E402
app.include_router(_internal_tools_router)

# Инструменты `aifa-engine` для ouroboros (CONTRACTS.md §3, ADR-0028): токен, привязка к ходу.
from finengine.assistant.tool_api import router as _ouro_tools_router  # noqa: E402
app.include_router(_ouro_tools_router)

# Кэш первой стадии извлечения: комната запечатана и живёт 18 минут, поэтому
# помнит за неё бэкенд (см. шапку модуля — там же, почему ключ по отпечатку
# промпта, а не по документу).
from finengine.api.internal_compression_cache import router as _compression_cache_router  # noqa: E402
app.include_router(_compression_cache_router)

# Внешний срез для разбора: комната запечатана, новости ищет бэкенд по её
# просьбе (имя компании знает классификация, то есть первый шаг в комнате).
from finengine.api.internal_external import router as _external_router  # noqa: E402
app.include_router(_external_router)

# Секрет внутренних роутов — СКАЗАТЬ ПРИ СТАРТЕ, а не на первом запросе
# пользователя: пустой секрет либо отключает проверку целиком (строгость снята),
# либо кладёт все прогоны станции (строгость поднята). И то и другое читается по
# логу за секунду, а по симптому — за полдня. Разбор: `api/internal_auth`.
from finengine.api.internal_auth import warn_if_unconfigured  # noqa: E402
warn_if_unconfigured()

# #276 шаг 1: чтение исхода прогона, переживающего закрытую вкладку. Аддитивно:
# при выключенном хранилище (дефолт) ручки отвечают 404, как отсутствующий прогон.
from finengine.api.run_outcomes import router as _run_outcomes_router  # noqa: E402
app.include_router(_run_outcomes_router)

# Сетка xlsx-источника (/projects/*/sources/*/sheet). Импорт модульный, а не
# ленивый как у соседних sources-ручек: константы капов стоят в дефолтах ручки.
from finengine.api import sheet_view  # noqa: E402


@app.on_event("startup")
async def _loop_lag_watchdog() -> None:
    """Замер для Р17: кто виноват в «Связь оборвалась» — сеть или наша петля.

    ФАКТЫ, ради которых он заведён (31.08.2026). Сервер шлёт кадр-биение SSE
    каждые 15 с; сторож фронта рвёт после 60 с тишины; за 90 минут живого
    прогона стрим переподключался четыре раза. Значит биение реально пропадало
    дольше минуты — а рождается оно В СОБЫТИЙНОЙ ПЕТЛЕ. Занята петля — нет ни
    кадров, ни событий, и фронт честно объявляет обрыв.

    Сторож меряет ДРЕЙФ: спит секунду и смотрит, сколько прошло на самом деле.
    Разница больше трёх секунд — петлю кто-то держал, и мы пишем СКОЛЬКО.
    По этим строкам следующий обрыв владельца превращается из «что происходит?»
    в имя виновника: время в логе сверяется с тем, что бэкенд делал в ту секунду.

    Наблюдательный код: поведения не меняет, событий не шлёт, потому и без
    отдельного стража.
    """
    import asyncio  # noqa: PLC0415

    async def _мерить() -> None:
        петля = asyncio.get_running_loop()
        было = петля.time()
        while True:
            await asyncio.sleep(1.0)
            стало = петля.time()
            дрейф = стало - было - 1.0
            if дрейф > 3.0:
                logging.getLogger(__name__).warning(
                    "событийная петля была занята ~%.1f с — биения SSE в это "
                    "время не рождались, фронт мог объявить обрыв (Р17)", дрейф)
            было = стало

    asyncio.get_running_loop().create_task(_мерить(), name="loop-lag-watchdog")


@app.on_event("startup")
async def warmup():
    """Seed admin user + migrate legacy docs.

    The embedding model is no longer pre-loaded here. After the full-context
    migration, uploads no longer embed PDFs (text sidecar instead of vectors).
    The embedder still lazy-loads on demand for legacy chat fallback only.
    Pre-loading made startup hang for ~5 minutes when HuggingFace was
    slow / the proxy flaky.
    """
    # ПЕРВЫМ ДЕЛОМ И ДО ВСЕГО: несколько воркеров без общей шины — тихий
    # разъезд хода. Падаем громко, а не работаем наполовину.
    from finengine.store.bus import проверить_готовность_к_репликам
    проверить_готовность_к_репликам()

    # Р17. 🔴 ДО 31.08.2026 ватчдог был ОПРЕДЕЛЁН И НЕ ПОДКЛЮЧЁН: обрыв
    # владельца прошёл без единой строки замера, и полдня диагноза ушло на
    # логи без глаз. Строка о заведении обязательна: молчащий лог должен
    # отличаться от незаведённого сторожа.
    #
    # `import logging` здесь вынужденный: ниже по телу warmup он уже есть
    # локальным, поэтому имя локально ВО ВСЕЙ функции — обращение к
    # модульному до этой строки роняло старт (UnboundLocalError, поймано
    # выкаткой 31.08: бэкенд ушёл в рестарт-петлю).
    import logging  # noqa: PLC0415
    await _loop_lag_watchdog()
    logging.getLogger(__name__).info("Р17: ватчдог событийной петли заведён")

    bootstrap_admin()

    # Слушатель работы тулов — на КАЖДОЙ реплике и с самого старта. Тул хода,
    # идущего здесь, может быть запрошен у соседа: он перешлёт работу по шине,
    # и услышать её обязаны мы. Лениво поднимать нельзя — реплика, которая
    # сама тулов не начинала, тогда молчала бы, а тикет висел бы «running».
    from finengine.api.internal_tools import запустить_слушателя
    запустить_слушателя()

    # Mark any runs that died with the previous container as session-lost.
    # Their journal files survived the restart but the in-memory
    # `_RunState` is gone — without this, the frontend would spin forever
    # waiting for events that will never come. Now covers every run kind
    # (chain / extract / quick / chat / synth) via the generalised
    # orchestrator.
    try:
        from finengine.skills.run_orchestrator import recover_orphan_runs
        counts = recover_orphan_runs()
        if counts:
            import logging
            total = sum(counts.values())
            logging.getLogger(__name__).info(
                "Marked %d orphan run(s) as session-lost on startup: %s",
                total, counts,
            )
    except Exception:
        pass

    # Initialize News pipeline storage + start the daily-trigger scheduler.
    # Scheduler is a long-running background task; cancelled cleanly on shutdown.
    # 🔴 init_schema — ВНЕ try: «база не настроена» обязана валить старт, как
    # bootstrap_admin строкой выше, а не тонуть в логе «failed to start
    # scheduler». Пока отказ глотался, контур без адресов поднимался «здоровым»
    # и узнавали об этом по пустому дайджесту. try остаётся только вокруг
    # запуска фоновой задачи: её отказ не должен ронять API.
    from finengine.skills.news import db as _news_db, scheduler as _news_sched
    import asyncio as _asyncio
    _news_db.init_schema()
    try:
        app.state.news_scheduler_task = _asyncio.create_task(_news_sched.run_forever())
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("news: failed to start scheduler")

    # Initialize Scenarios storage + start the scenario scheduler (same pattern
    # as the news digest: a long-running background task, cancelled on shutdown).
    # То же правило: init_schema и sweep — вне try (оба ходят в engine() и
    # обязаны валить старт при ненастроенном контуре), try — вокруг задачи.
    from finengine.skills.scenarios import store as _scn_store, scheduler as _scn_sched
    _scn_store.init_schema()
    # Прогоны сценариев живут в этом же процессе (scheduler-таск + manual
    # run через asyncio.create_task) — рестарт убивает их до финализации
    # статуса в runner.py. 17.08.2026 деплой так оставил ir_calendar в
    # last_status='running' навсегда: вечное «выполняется» в админке.
    _scn_store.sweep_stale_running()
    try:
        app.state.scenario_scheduler_task = _asyncio.create_task(_scn_sched.run_forever())
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("scenarios: failed to start scheduler")


@app.on_event("shutdown")
async def _stop_schedulers():
    for attr in ("news_scheduler_task", "scenario_scheduler_task"):
        task = getattr(app.state, attr, None)
        if task:
            task.cancel()
            try:
                await task
            except Exception:  # noqa: BLE001
                pass


@app.post("/skills/news_digest/run")
async def skills_news_digest_run(
    fresh: bool = False,
    since_hours: int | None = None,
    top_n: int | None = None,
    user: User = Depends(get_current_user),
):
    """Run the Daily News ETL pipeline. Returns the markdown digest +
    cost/time metadata. If `fresh=False` (default) AND we have a cached
    digest <24h old, return that without re-running."""
    from finengine.skills.news.pipeline import (
        get_cached_digest, run_daily_news_pipeline,
    )

    if not fresh:
        cached = get_cached_digest(user_id=user.id)
        if cached is not None:
            cached["from_cache"] = True
            return cached

    # Умолчания были жёсткими (48ч и 60 кандидатов) и передавались ВСЕГДА,
    # перебивая и сохранённые настройки владельца, и настройку потолка. Теперь
    # None означает «взять сохранённое», а явный параметр по-прежнему сильнее.
    from finengine.skills.news.pipeline import settings_for_user
    _own = settings_for_user(user.id)
    result = await run_daily_news_pipeline(
        since_hours=int(since_hours or _own.get("since_hours") or 48),
        top_n_candidates=top_n,
        source_groups=_own.get("source_groups") if isinstance(
            _own.get("source_groups"), dict) else None,
        macro_mode=str(_own.get("macro_mode") or "both"),
        user_id=user.id,
    )
    result["from_cache"] = False
    return result


@app.get("/skills/news_digest/latest")
async def skills_news_digest_latest(user: User = Depends(get_current_user)):
    """Return the most recent finished digest from cache (no pipeline run)."""
    from finengine.skills.news.pipeline import get_cached_digest
    cached = get_cached_digest(max_age_hours=72, user_id=user.id)
    if cached is None:
        return {"error": "no cached digest available"}
    return cached


@app.get("/admin/trace/{session_id}")
def admin_trace(session_id: str, user: User = Depends(get_current_user)):
    """Full event timeline for a session — every llm/tool/error event in
    chronological order, full payloads (no truncation). For debugging
    why a digest went sideways."""
    from finengine.observability import read_session
    events = read_session(user_id=user.id, session_id=session_id)
    return {"session_id": session_id, "count": len(events), "events": events}


@app.get("/projects/{project_id}/usage")
def project_usage(project_id: str, user: User = Depends(get_current_user)):
    """Accumulated token spend for a project. Used by the chat UI to show
    'spent in this project: $X.XX' under the composer."""
    from finengine.llm.traces import project_totals
    return project_totals(user_id=user.id, project_id=project_id)


@app.get("/admin/llm-spend")
async def admin_llm_spend(user: User = Depends(get_current_user)):
    """Cross-check our local trace totals against what OpenRouter actually
    billed. If the sums don't match → there's still a hidden call path
    bypassing the trace logger OR an OpenRouter key we forgot to register.

    OpenClaw gateway stores its own OpenRouter key separately from our
    backend's `.env` — chat/digest path bills the gateway key, while
    extract/chains/synthesis bills the backend key. So we need to poll
    BOTH and sum, otherwise we under-count.

    Configure additional keys via env:
        OPENROUTER_API_KEY                — backend key (auto-detected)
        FINENGINE_OPENROUTER_KEYS_CSV     — comma-separated extras
                                           (e.g. the OpenClaw gateway key)
    """
    import os
    import httpx

    from finengine.config import settings as _s
    from finengine.store import journals as _j

    # Обходим все проекты владельца и суммируем расход.
    local_total = 0.0
    by_kind: dict[str, float] = {}
    for проект in _j.keys(_j.LLM_TRACES, owner=user.id):
        for e in _j.read(_j.LLM_TRACES, owner=user.id, key=проект):
            cost = float(e.get("cost_usd", 0) or 0)
            local_total += cost
            k = e.get("kind", "other") or "other"
            by_kind[k] = by_kind.get(k, 0.0) + cost

    # Aggregate spend across every OpenRouter key we know about. Each
    # key gets its own /v1/key call; we return per-key labels so the
    # operator can spot which key is leaking. Full key values are
    # NEVER returned — we only emit OpenRouter's own `label` field
    # (which is always a redacted tail like `sk-or-v1-...f8d`).
    keys: list[str] = []
    if _s.openrouter_api_key:
        keys.append(_s.openrouter_api_key)
    extras = os.environ.get("FINENGINE_OPENROUTER_KEYS_CSV", "")
    for k in extras.split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)

    per_key: list[dict] = []
    openrouter_total = 0.0
    async with httpx.AsyncClient(timeout=10.0) as cli:
        for k in keys:
            entry: dict = {"label": None, "usage_usd": None,
                           "limit_usd": None, "limit_remaining_usd": None,
                           "error": None}
            try:
                r = await cli.get(
                    "https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {k}"},
                )
                if r.status_code == 200:
                    d = (r.json() or {}).get("data") or {}
                    entry["label"] = d.get("label")
                    entry["usage_usd"] = d.get("usage")
                    entry["limit_usd"] = d.get("limit")
                    entry["limit_remaining_usd"] = d.get("limit_remaining")
                    if isinstance(d.get("usage"), (int, float)):
                        openrouter_total += float(d["usage"])
                else:
                    entry["error"] = f"openrouter {r.status_code}"
            except httpx.HTTPError as e:
                entry["error"] = str(e)
            per_key.append(entry)

    return {
        "local_total_usd": round(local_total, 4),
        "local_by_kind": {k: round(v, 4) for k, v in by_kind.items()},
        "openrouter_total_usd": round(openrouter_total, 4) if per_key else None,
        "openrouter_keys": per_key,
        "keys_registered": len(keys),
    }


@app.get("/health")
def health():
    # 🔴 БЕЗ ПОХОДОВ В БАЗУ. Счётчики документов делали health двумя COUNT(*)
    # на каждый тик docker healthcheck — а зависшая (не упавшая) база вешала бы
    # сам health и роняла контейнер по таймауту проверки. Потребителей у
    # счётчиков не было ни во фронте, ни в скриптах (сверено ревизией 27.08).
    from finengine.policy.killswitch import is_killed
    killed, reason = is_killed()
    return {
        "status": "ok",
        "agent_killed": killed,
        "agent_killed_reason": reason,
    }


@app.get("/_sentry-test")
def _sentry_test():
    """Trigger a test exception so we can verify Sentry is wired up."""
    raise RuntimeError("sentry-test: backend integration check")


def _parse_doc_in_background(
    file_ref: str,
    doc_id: str,
    filename: str,
    ocr_engine: str = "pymupdf",
) -> None:
    """Разбор документа в фоне. Файл берётся из хранилища ИЛИ с тома.

    🔴 `file_ref` — КЛЮЧ ОБЪЕКТА ИЛИ ПУТЬ, и различать их здесь не надо:
    `document_file` решает это одним местом на весь код и убирает временную
    копию сам — включая случай, когда разбор упал. Раньше сюда приходил
    `Path`, и после переезда документов в объектное хранилище он указывал бы
    в никуда.

    Parse PDF → text sidecar after upload returns. Updates metadata when
    done (auto-detected company name). Errors are logged but do not surface
    to the user — the .txt simply doesn't appear, and extract returns
    "обрабатывается" until it does.

    `ocr_engine` selects the text-extraction backend:
      "pymupdf" — built-in PyMuPDF/pdfplumber (fast, weak on tables)
      "idp"     — IDP-OCR cloud (per-page PNG, strong tables, ~15× slower)
    """
    from finengine.store.blobs import document_file  # noqa: PLC0415

    # ⚠️ ExitStack, А НЕ `with` ВОКРУГ ТЕЛА, И ЭТО НЕ ЛЕНЬ. Обёртка `with`
    # потребовала бы переотступить сотню строк разбора — правку, в которой
    # ошибка не видна глазом и не ловится тестом. Стек даёт ту же гарантию
    # (копия убирается на любом выходе, включая исключение) и не трогает
    # существующий код.
    стек = ExitStack()
    try:
        meta = get_store().get_document(doc_id)
        if not meta:
            return
        file_path = стек.enter_context(document_file(file_ref))
        if filename.lower().endswith(".pdf"):
            if ocr_engine == "idp":
                # IDP path: every page → PNG → OCR cloud → markdown.
                # Result.full_text is the merged per-page markdown ready to
                # write into the sidecar. Slower (~15-20× PyMuPDF) but
                # finds bank/IFRS tables PyMuPDF misses (Layer 1 confirmed).
                import asyncio as _aio
                from finengine.skills.aifa_baseline.idp_ocr_client import call_idp_ocr
                from finengine.store.blobs import document_idp_sink
                with document_idp_sink(doc_id) as raw_path:
                    ocr_result = _aio.run(
                        call_idp_ocr(file_path, save_raw_to=raw_path,
                                     parallel=settings.idp_ocr_parallel)
                    )
                full_text = ocr_result.full_text
                # Число страниц ставит только pymupdf-ветка (parse_pdf), и после
                # облачного OCR документ приезжал с total_pages=0. Следствие
                # видно на экране: лента страниц не собирается (неизвестно,
                # сколько паспарту рисовать), и прыгать по ссылке «PDF стр. N»
                # из анализа некуда — ровно то, ради чего страницу и везли
                # (#157). Берём ФИЗИЧЕСКОЕ число страниц файла (его считает
                # PyMuPDF в начале OCR), а не число успешно распознанных: лента
                # нумерует страницы ФАЙЛА, и пропуск неудавшейся страницы сдвинул
                # бы все последующие ссылки на единицу.
                meta.total_pages = int(ocr_result.diag.get("pages_total") or 0)
            else:
                # Default PyMuPDF/pdfplumber pipeline (unchanged).
                ch = parse_pdf(file_path, meta, settings.chunk_size, settings.chunk_overlap)
                relevant = [c for c in ch if c.chunk_type in ("text", "financial_table", "table")]
                relevant.sort(key=lambda c: (c.page or 0, 0 if c.chunk_type == "financial_table" else 1))
                seen = set()
                uniq: list = []
                for c in relevant:
                    key = (c.page, c.chunk_type, c.content[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append(c)
                full_text = "\n\n".join(c.content for c in uniq)
        elif filename.lower().endswith((".txt", ".md")):
            full_text = file_path.read_text(encoding="utf-8")
        else:
            return

        if not full_text or not full_text.strip():
            return

        # Публикация атомарна внутри хранилища: поллинг статуса и тул никогда
        # не увидят полу-записанный текст и не сочтут его готовым.
        from finengine.store.blobs import save_document_text  # noqa: PLC0415

        save_document_text(doc_id, full_text)
        # Stamp which backend produced this sidecar — useful for diag and
        # for the per-doc "Источник OCR" badge in the UI.
        try:
            meta.ocr_engine = ocr_engine
            get_store().add_document(meta, [])
        except Exception:
            pass

        if not meta.company_name:
            from finengine.skills.bulk_extractor import _detect_company_from_text
            detected = _detect_company_from_text(full_text)
            if detected:
                meta.company_name = detected
                get_store().add_document(meta, [])  # upsert metadata

        # МЕТАКАРТА (#189). Здесь и только здесь: документ уже приведён к тексту
        # («сначала текст, потом классификация» — решение владельца), и это
        # единственный раз за жизнь документа. Замеры делает код, вид/эмитента/
        # период называет модель одним обращением.
        #
        # Карта НЕ СМЕЕТ ронять приём документа: не получилась — в ней остаются
        # замеры и причина, документ принят как обычно. Молчать про неудачу тоже
        # нельзя, поэтому причина ложится в саму карту, а не только в лог.
        if not meta.meta_card:
            import asyncio as _aio_card
            import logging as _log_card

            from finengine.skills.meta_card import build as _build_card
            try:
                meta.meta_card = _aio_card.run(_build_card(full_text))
                get_store().add_document(meta, [])
                _log_card.getLogger(__name__).info(
                    "метакарта %s: %s", doc_id,
                    {k: meta.meta_card.get(k) for k in ("doc_kind", "issuer", "period", "error")})
            except Exception:
                _log_card.getLogger(__name__).exception("метакарта %s не снята", doc_id)
    except Exception as e:
        import logging
        logging.exception("Background parse failed for %s: %s", doc_id, e)
    finally:
        # Временная копия убирается ВСЕГДА, включая падение разбора:
        # иначе /tmp наполнялся бы отчётами по пять мегабайт.
        стек.close()


@app.post("/documents/upload")
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    company_name: str = Form(""),
    period: str = Form(""),
    doc_type: str = Form(""),
    company_type: str = Form("unknown"),
    ocr_engine: str = Form("idp"),
    # Новый экран грузит файл СРАЗУ, а распознавать просит только когда
    # пользователь отправил документ в чат: до этого момента тратить облачный
    # OCR не на что (пользователь может передумать или добавить ещё файлы).
    defer_parse: str = Form(""),
    user: User = Depends(get_current_user),
):
    """Upload a document (PDF/TXT/MD).

    The endpoint returns as soon as the file is on disk — the actual PDF
    parse + .txt sidecar generation runs as a background task. The UI can
    show the document and PDF viewer immediately. /extract/stream waits for
    the .txt to appear and otherwise emits a "still processing" error.
    """
    try:
        parsed_doc_type = DocType(doc_type) if doc_type else DocType.OTHER
        parsed_company_type = CompanyType(company_type) if company_type else CompanyType.UNKNOWN
    except ValueError as exc:
        field = "doc_type" if doc_type and doc_type not in {item.value for item in DocType} else "company_type"
        enum_type = DocType if field == "doc_type" else CompanyType
        allowed = ", ".join(item.value for item in enum_type)
        raise HTTPException(
            status_code=422,
            detail=f"Недопустимый {field}; допустимые значения: {allowed}",
        ) from exc

    doc_id = str(uuid.uuid4())
    filename = file.filename or "document.pdf"
    # Имя НА ДИСКЕ капим по БАЙТАМ utf-8: кириллица = 2 байта/символ, длинные
    # названия банковских отчётов пробивают лимит ФС в 255 байт → OSError 36 →
    # 500 (реальный инцидент: «Промежуточная бухгалтерская … 15.05.2025г..pdf»).
    # Оригинальное имя целиком остаётся в DocumentMeta.filename (для UI/Excel).
    _stem, _dot, _ext = filename.rpartition(".")
    _stem = _stem or filename
    _suffix = f".{_ext}" if _dot else ""
    _budget = 200 - len(doc_id) - 1 - len(_suffix.encode("utf-8"))
    _stem_b = _stem.encode("utf-8")[:max(_budget, 8)]
    _safe_stem = _stem_b.decode("utf-8", errors="ignore")
    file_path = settings.docs_dir / f"{doc_id}_{_safe_stem}{_suffix}"

    # Save file synchronously — small enough that this is fast.
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # Срез 10: имя файла не является версией документа. Content hash
    # фиксируется до object-store move и используется Case для точечной
    # invalidation/reuse между сообщениями и service instances.
    _sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()

    # ВОРОТА НА ВХОДЕ. Битый файл надо отбивать здесь, а не через пять минут в
    # OCR. Реальный случай (01.08.2026): пользователь загрузил указатель Git LFS
    # вместо PDF — 132 байта текста «version https://git-lfs.github.com/...».
    # Система приняла его как документ, OCR упал на pymupdf, а ход всё равно ждал
    # распознавания до таймаута и сказал «PDF не удалось обработать за 5 минут —
    # попробуйте загрузить ещё раз», отправив человека грузить тот же мусор по
    # кругу. Проверка стоит миллисекунды и снимает целый класс таких тупиков.
    if file_path.suffix.lower() == ".pdf":
        _size = file_path.stat().st_size
        with open(file_path, "rb") as _fh:
            _head = _fh.read(1024)
        _why = ""
        if _head.startswith(b"version https://git-lfs.github.com/"):
            _why = ("это указатель Git LFS, а не сам файл — в репозитории нужно "
                    "выполнить `git lfs pull`, либо загрузите настоящий PDF")
        elif not _head.lstrip()[:5].startswith(b"%PDF-"):
            _why = "файл не начинается с сигнатуры %PDF- — это не PDF"
        elif _size < 1024:
            _why = f"файл подозрительно мал ({_size} байт) — похоже, выгрузился не целиком"
        if _why:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"«{filename}»: {_why}")

    # Lightweight metadata, register before background parse — UI sees the doc.
    # ОБЪЕКТ УЕЗЖАЕТ В ХРАНИЛИЩЕ, А ЗАПИСЬ ДЕРЖИТ КЛЮЧ (architecture.md 3.1:
    # «загрузки — в объектное хранилище… никогда на диск»). Файл на диске выше
    # нужен был только воротам: чтобы отбить указатель LFS и не-PDF, надо
    # прочитать первые байты. Дальше он лишний.
    from finengine.store.blobs import document_key, get_blob_store  # noqa: PLC0415

    _хранилище = get_blob_store()
    if hasattr(_хранилище, "ensure_bucket"):
        _хранилище.ensure_bucket()
    _ключ = document_key(doc_id, filename)
    with open(file_path, "rb") as _src:
        _хранилище.put(_ключ, _src)
    file_path.unlink(missing_ok=True)

    meta = DocumentMeta(
        doc_id=doc_id,
        filename=filename,
        doc_type=parsed_doc_type,
        company_name=company_name,
        company_type=parsed_company_type,
        period=period,
        file_path=_ключ,
        owner_id=user.id,
        sha256=_sha256,
    )
    get_store().add_document(meta, [])

    # Normalise OCR backend choice — anything other than "idp" falls back
    # to the safe default. Stamped on the meta so we know which engine
    # produced the sidecar later.
    ocr_choice = "idp" if ocr_engine.lower() == "idp" else "pymupdf"
    meta.ocr_engine = ocr_choice
    get_store().add_document(meta, [])

    # Schedule the heavy work for after the response is sent — если разбор не
    # отложен клиентом.
    deferred = defer_parse.strip().lower() in ("1", "true", "yes")
    if not deferred:
        background.add_task(_parse_doc_in_background, _ключ, doc_id, filename, ocr_choice)

    return {
        "doc_id": doc_id,
        "filename": filename,
        "chunks": 0,
        "pages": meta.total_pages,
        "doc_type": meta.doc_type,
        "language": meta.language,
        "processing": not deferred,
        "deferred": deferred,
    }


@app.post("/documents/{doc_id}/parse")
def start_document_parse(
    doc_id: str,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """Запустить разбор (OCR) документа, загруженного с defer_parse.

    Идемпотентно: если текст уже готов, повторный вызов ничего не делает —
    ход в чате может дёрнуть ручку для каждого приложенного документа, не
    выясняя предварительно, чей разбор уже прошёл.
    """
    meta = get_store().get_document(doc_id)
    if not meta or (meta.owner_id and meta.owner_id != user.id):
        raise HTTPException(status_code=404, detail="Document not found")

    from finengine.store.blobs import document_text_exists  # noqa: PLC0415

    if document_text_exists(doc_id):
        return {"doc_id": doc_id, "processing": False, "ready": True}

    background.add_task(
        _parse_doc_in_background,
        meta.file_path,
        doc_id,
        meta.filename,
        getattr(meta, "ocr_engine", None) or "pymupdf",
    )
    return {"doc_id": doc_id, "processing": True, "ready": False}


@app.get("/admin/documents/{doc_id}/idpocr.json")
def download_idp_ocr_raw(doc_id: str, user: User = Depends(get_current_user)):
    """Admin-only download of the raw IDP-OCR response for a document.

    Saved by aifa_baseline on every successful OCR call; use it to inspect
    what the OCR actually returned (markdown, tables, page count) when an
    AIFA baseline run lands few values.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    # Порядок версий (сначала v2 — нынешний постраничный конвейер, потом v1 —
    # прогоны до смены клиента OCR) держит `store/blobs`, а не этот роут.
    from finengine.store.blobs import document_idp_raw

    сырьё = document_idp_raw(doc_id)
    if сырьё is None:
        raise HTTPException(
            status_code=404,
            detail="No IDP-OCR response saved. Run extract in AIFA baseline mode first.",
        )
    return Response(content=сырьё, media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="{doc_id}.idpocr.json"'})


@app.get("/admin/extract-runs.jsonl")
def download_extract_runs(user: User = Depends(get_current_user)):
    """Admin-only download of every extraction run captured so far.

    Each line in the response is one run record (see run_logger.log_run for
    the shape). Used by the benchmark builder + ad-hoc analysis: filter by
    `arch` (finengine / aifa_dev / aifa_baseline) and `company_type`,
    aggregate found / total against GT.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    # Журнал живёт в общем хранилище; выгрузку собираем строками, а не отдаём
    # файлом. Формат для потребителя тот же — NDJSON.
    from finengine.skills.run_logger import ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, прочитать, runs_path

    строки = прочитать(ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, файл=runs_path())
    тело = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in строки)
    return Response(content=тело, media_type="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="extract_runs.jsonl"'})


@app.get("/admin/chat-runs.jsonl")
def download_chat_runs(user: User = Depends(get_current_user)):
    """Admin-only download of the chat-turn audit log. Each line is one
    user→agent turn (prompt + full response + session/user/doc context).
    Used to debug what real users are asking and what the agent answered."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    from finengine.skills.run_logger import ЖУРНАЛ_ЧАТА, chat_runs_path, прочитать

    строки = прочитать(ЖУРНАЛ_ЧАТА, файл=chat_runs_path())
    тело = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in строки)
    return Response(content=тело, media_type="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="chat_runs.jsonl"'})


@app.get("/admin/runs")
def list_extract_runs(
    user: User = Depends(get_current_user),
    limit: int = 200,
):
    """Admin-only: parsed extract_runs.jsonl as JSON, with broken-flag and
    OCR-availability hints, for the /admin/runs UI page.

    Each row carries enough info to drive the UI: timestamp, doc_id,
    arch, model, found/total, elapsed (in minutes), broken flag with
    reason, an `idpocr_available` flag (so the UI can disable the link
    when no cache exists), and the full diag for the expand-row view.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    from finengine.skills.run_logger import ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, прочитать, runs_path

    out: list[dict] = []
    записи = прочитать(ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, файл=runs_path())
    if not записи:
        return {"runs": out, "total": 0}

    # newest-first, then trim to limit
    for r in reversed(записи):
        # Broken-run classification — same rules as
        # tests/build_unified_benchmark.py:classify_broken
        broken = False
        reason = "OK"
        if r.get("error"):
            broken, reason = True, f"error: {str(r['error'])[:80]}"
        else:
            found = int(r.get("found") or 0)
            total = int(r.get("total") or 0)
            metrics = r.get("metrics") or []
            if total == 0:
                broken, reason = True, "total=0"
            elif found == 0:
                broken, reason = True, "found=0"
            elif len(metrics) < 0.3 * total:
                broken, reason = True, f"truncated: {len(metrics)}/{total} metrics"
            else:
                samples = (r.get("diag") or {}).get("samples") or []
                if samples:
                    n_err = sum(1 for s in samples if s.get("reason") == "llm_error" or s.get("error"))
                    if n_err > 0.5 * len(samples):
                        broken, reason = True, f"{n_err}/{len(samples)} sample LLM errors"

        from finengine.store.blobs import document_idp_exists

        doc_id = r.get("doc_id") or ""
        idpocr_available = document_idp_exists(doc_id)

        elapsed_s = float(r.get("elapsed_s") or 0)
        out.append({
            "ts": r.get("ts"),
            "git_ver": r.get("git_ver") or "",
            "git_sha": r.get("git_sha") or "",
            "doc_id": doc_id,
            "filename": r.get("filename"),
            "arch": r.get("arch"),
            "model_id": r.get("model_id"),
            "company_type": r.get("company_type"),
            "company_name": r.get("company_name"),
            "found": r.get("found"),
            "total": r.get("total"),
            "elapsed_min": round(elapsed_s / 60.0, 2),
            "broken": broken,
            "broken_reason": reason,
            "idpocr_available": idpocr_available,
            "diag": r.get("diag") or {},
            "error": r.get("error"),
        })
        if len(out) >= limit:
            break

    return {"runs": out, "total": len(записи)}


class MetricsExportRow(BaseModel):
    # 🔴 ЗНАЧЕНИЯ — НЕ ТОЛЬКО ЧИСЛА (#142, найдено 17.08 по логу два 422 с
    # адреса владельца). Фронт держит MetricValue = number | string | null:
    # движок отдаёт и сырые ячейки строками, и «да/нет» аудиторского мнения.
    # Прежний `float | None` валил ВЕСЬ запрос 422-й на первой же строковой
    # величине, фронт глотал ошибку в .catch — и выбор роли в выпадашке
    # «Извлечь метрики» молча не менял таблицу. Симптом жил днями, потому что
    # отказ не был виден нигде, кроме access-лога.
    enum: str
    name_ru: str = ""
    value_current: float | str | bool | None = None
    value_prev: float | str | bool | None = None
    period_values: list[dict] = Field(default_factory=list)
    unit: str = ""
    page: object | None = None
    source_form: str = ""
    source_text: str = ""
    segment: str = ""
    dimension: str = ""
    is_calculated: bool = False
    formula: str = ""
    # Числа, подставленные в формулу (Б5). Без объявления поля pydantic молча
    # выбрасывает лишний ключ, и до книги роли происхождение расчёта не едет.
    inputs: list[dict] = Field(default_factory=list)

    # 🔴 ТА ЖЕ БЕДА, ЧТО У ЗНАЧЕНИЙ (#142), НО У ТЕКСТОВЫХ ПОЛЕЙ. Найдено
    # 20.08 проигрыванием НАСТОЯЩЕЙ таблицы прогона (Северсталь, 122 строки из
    # базы) через эту схему: 302 ошибки валидации — `segment` и `dimension`
    # null во ВСЕХ 122 строках, `source_form` в 58. То есть выбор роли в
    # выпадашке отбивался 422-й ВСЕГДА, при любой роли и любом документе, а
    # таблица оставалась прежней — жалоба владельца «роль меняется, таблица не
    # перестраивается».
    #
    # Пустая строка и отсутствие значения для этих полей — одно и то же, и
    # строка прогона имеет полное право нести null: `_ROW_KEYS` объявляет
    # `segment`/`dimension` именно так.
    @field_validator("name_ru", "unit", "source_form", "source_text",
                     "segment", "dimension", "formula", mode="before")
    @classmethod
    def _absent_text_is_empty(cls, value: object) -> object:
        return "" if value is None else value

    # ТА ЖЕ БЕДА, ЧТО У ТЕКСТОВЫХ ПОЛЕЙ (#142), НО У СПИСКА. Прогоны, снятые
    # до Б5, входов не несут, и `_row_body` отдаёт по этому ключу null —
    # без приведения выбор роли отбивался бы 422-й на КАЖДОЙ старой таблице.
    @field_validator("inputs", mode="before")
    @classmethod
    def _absent_inputs_are_empty(cls, value: object) -> object:
        return [] if value is None else value


class MetricsExportRequest(BaseModel):
    company: str = ""
    period_current: str = ""
    period_prev: str = ""
    unit: str = ""
    rows: list[MetricsExportRow]


class MetricsRoleFormatRequest(BaseModel):
    company_name: str = ""
    company_type: str = ""
    role: str = ""
    period_current: str = ""
    period_prev: str = ""
    unit: str = ""
    rows: list[MetricsExportRow]


@app.post("/metrics/format-role")
def metrics_format_role(
    req: MetricsRoleFormatRequest, user: User = Depends(get_current_user)
):
    """Пересобрать уже извлечённые метрики в раскладку под роль (порт из
    AIFA_RFA): фронт шлёт строки с period_values → получает FormattedTable
    (dgr широкая / cib длинная / die сегменты / …) для рендера."""
    from finengine.roles.access import (
        default_role_for_user,
        normalize_extraction_role,
        user_can_extract_as,
    )
    from finengine.roles.formatter import build_formatted_table
    from finengine.roles.scope import build_role_plan

    requested_role = normalize_extraction_role(
        req.role, default=default_role_for_user(user)
    )
    if req.role and not user_can_extract_as(user, requested_role):
        raise HTTPException(
            status_code=403, detail="Role is not available for this user"
        )

    plan = build_role_plan(requested_role, req.company_type)
    metrics = [r.model_dump() for r in req.rows]
    meta = {
        "period_current": req.period_current,
        "period_prev": req.period_prev,
        "unit": req.unit,
        "company_type": req.company_type,
        "company_name": req.company_name,
    }
    table = None
    if settings.role_formatters_enabled and plan.config:
        table = build_formatted_table(plan=plan, metrics=metrics, meta=meta)

    return {
        "role": plan.role,
        "role_label": plan.role_label,
        "company_type": plan.company_type,
        "role_format": plan.format,
        "output_enums": (
            sorted(plan.output_enums) if plan.output_enums is not None else None
        ),
        "formatted": table,
    }


@app.post("/metrics/export.xlsx")
def metrics_export_xlsx(req: MetricsExportRequest, user: User = Depends(get_current_user)):
    """Render the user's extracted-metrics table to a downloadable .xlsx.

    The CSV path lives entirely in the frontend (BOM + utf-8). Real Excel
    needs a proper xlsx, so we shape it here with openpyxl — same dep we
    already use for the bench file.
    """
    import io
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse

    wb = openpyxl.Workbook()
    ws = wb.active
    if ws is None:
        ws = wb.create_sheet()
    ws.title = "Метрики"

    cur_label = req.period_current or "Текущий"
    prev_label = req.period_prev or "Предыдущий"
    headers = ["Метрика", "Enum", cur_label, prev_label, "Δ %", "Единица", "Calc", "Формула"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    def _delta(curr: float | None, prev: float | None) -> float | None:
        if curr is None or prev is None or prev == 0:
            return None
        return (curr - prev) / abs(prev) * 100

    for r in req.rows:
        d = _delta(r.value_current, r.value_prev)
        ws.append([
            r.name_ru or r.enum,
            r.enum,
            r.value_current,
            r.value_prev,
            None if d is None else round(d, 2),
            r.unit or req.unit or "",
            "Y" if r.is_calculated else "",
            r.formula or "",
        ])

    # Column widths
    widths = [42, 32, 14, 14, 10, 12, 6, 38]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    # Right-align numeric columns
    for col in (3, 4, 5):
        for cell in ws[get_column_letter(col)][1:]:
            cell.alignment = Alignment(horizontal="right")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_name = (req.company or "metrics").strip() or "metrics"
    fname = f"{safe_name}_{req.period_current or ''}.xlsx".replace(" ", "_")
    # Cyrillic filenames break a raw `filename="..."` header (Safari refuses
    # the response with "Load failed", Chrome strips characters silently).
    # RFC 6266 / 5987 says non-ASCII names must use `filename*=UTF-8''<pct>`,
    # with an ASCII fallback for older clients. quote() percent-encodes the
    # name; we also keep an ASCII-only `filename=` so dumb clients still get
    # something.
    from urllib.parse import quote
    ascii_fname = re.sub(r"[^\x20-\x7e]+", "_", fname) or "metrics.xlsx"
    disposition = (
        f'attachment; filename="{ascii_fname}"; '
        f"filename*=UTF-8''{quote(fname)}"
    )
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


class TableXlsxRequest(BaseModel):
    markdown: str
    filename: str = "export"


def _parse_markdown_table(md: str) -> list[list[str]]:
    """Extract the first GFM markdown table from `md` → list of rows. Strips the
    |---| separator row, unwraps [text](url) → url, drops **bold** / `code`."""
    def _clean(cell: str) -> str:
        s = cell.strip()
        m = re.match(r"^\[([^\]]*)\]\(([^)]*)\)$", s)
        if m:
            return (m.group(2) or m.group(1)).strip()  # prefer the URL
        return s.replace("**", "").replace("`", "").strip()

    rows: list[list[str]] = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        if re.fullmatch(r"\|[\s|:\-]+\|?", line):  # |---|---| separator
            continue
        rows.append([_clean(c) for c in line.strip("|").split("|")])
    return rows


@app.post("/export/table.xlsx")
def export_table_xlsx(req: TableXlsxRequest, user: User = Depends(get_current_user)):
    """Render the first markdown table in `req.markdown` to a downloadable .xlsx.
    Used by the chat message "Скачать" button (e.g. the IR-Calendar table)."""
    import io
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    from urllib.parse import quote

    rows = _parse_markdown_table(req.markdown)
    if not rows:
        raise HTTPException(status_code=400, detail="В ответе нет таблицы для экспорта")

    wb = openpyxl.Workbook()
    ws = wb.active
    if ws is None:
        ws = wb.create_sheet()
    ws.title = "Данные"
    for r in rows:
        ws.append(r)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    ncols = max((len(r) for r in rows), default=0)
    for i in range(1, ncols + 1):
        longest = max((len(str(r[i - 1])) for r in rows if i - 1 < len(r)), default=10)
        ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = ((req.filename or "export").strip() or "export").replace(" ", "_") + ".xlsx"
    ascii_fname = re.sub(r"[^\x20-\x7e]+", "_", fname) or "export.xlsx"
    disposition = f'attachment; filename="{ascii_fname}"; ' + f"filename*=UTF-8''{quote(fname)}"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


@app.get("/documents/{doc_id}/status")
def document_status(doc_id: str, user: User = Depends(get_current_user)):
    """Returns whether the .txt sidecar is ready (PDF parse finished)."""
    doc = get_store().get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")
    # Готово = текст ЕСТЬ И НЕПУСТ. Пустой не публикуется вовсе, а `document_text`
    # пустое не отдаёт — иначе чат говорил бы «распознан», а тул на пустом
    # тексте «ещё обрабатывается».
    from finengine.store.blobs import document_text_exists  # noqa: PLC0415

    ready = document_text_exists(doc_id)
    return {
        "doc_id": doc_id,
        "processing": not ready,
        "company_name": doc.company_name or "",
    }


_PREMIUM_MODEL_KEY_TO_ID = {
    "kimi": "moonshotai/kimi-k2.5",
    "glm": "z-ai/glm-5.1",
}
_PREMIUM_MODEL_IDS = set(_PREMIUM_MODEL_KEY_TO_ID.values())
_BASE_MODEL_ID = "qwen/qwen3.6-plus"


def _enforce_model_grant(model_id: str | None, user: User) -> str | None:
    """Return the model id to use, falling back to the base model if the user
    asked for a premium one without a matching grant. Admins keep full access.
    """
    if not model_id or model_id == _BASE_MODEL_ID:
        return model_id
    if user.is_admin:
        return model_id
    granted_keys = {k.strip() for k in (user.allowed_models or "").split(",") if k.strip()}
    granted_ids = {_PREMIUM_MODEL_KEY_TO_ID[k] for k in granted_keys if k in _PREMIUM_MODEL_KEY_TO_ID}
    if model_id in _PREMIUM_MODEL_IDS and model_id not in granted_ids:
        return _BASE_MODEL_ID
    return model_id


def _user_can_see(doc: DocumentMeta, user: User) -> bool:
    """Owner or admin (admins also see legacy owner_id=0 docs).

    getattr fallback handles documents that were indexed before the
    owner_id field existed — pydantic won't synthesise the default for
    unpickled objects, so direct attribute access throws AttributeError.
    """
    if user.is_admin:
        return True
    return getattr(doc, "owner_id", 0) == user.id


def _docs_of_the_turn(doc_ids: list[str], user: User) -> list[str]:
    """Документы хода, которые звавший ВПРАВЕ видеть, — в порядке заказа.

    ЦЕНА ДЕФЕКТА (Б8.1, 19.08.2026). Список документов хода собирался прямо из
    тела запроса — `request.doc_ids` + `active_doc_id` — и без единой проверки
    уходил в `station_turn.arm`, а тот открывает по нему скоуп нонса
    (`internal_docs.register_nonce_docs`). Скоуп — единственное, что решает,
    какой текст комната получит по `/internal/doc-text`: сам роут проверяет
    токен демона, форму doc_id и членство в скоупе, но НЕ владельца
    (см. шапку `internal_docs.py`: «Owner-гейт остаётся в диспетчере ДО заказа
    воркера»). Диспетчером был этот ход — и гейта у него не было. Подставив
    чужой doc_id в тело `POST /chat/turns`, любой вошедший пользователь
    вытягивал в свою комнату текст чужого отчёта. `docs/rules/security.md`
    §3.1: отсутствие проверки владельца — блокер, а не улучшение.

    ЧУЖОЕ ВЫБРАСЫВАЕТСЯ МОЛЧА, А НЕ ОТБИВАЕТ ХОД, — потому что так делают
    соседи по этому же файлу: одиночный НАЗВАННЫЙ документ отвечает 404
    (`/documents/{doc_id}`, `/documents/{doc_id}/status`, удаление), а СПИСОК
    сужен без слова (`/documents`: `owner_id` уходит фильтром в сам SQL-запрос).
    Здесь список. Решающий довод — `agent._build_context`: чат-путь ТОГО ЖЕ
    хода уже отбрасывает чужие документы через `continue`, и отбей мы ход
    целиком, две половины одного хода разошлись бы в поведении.

    Документ, которого нет в индексе, тоже выбрасывается: владельца у него не
    спросить, а fail-open здесь и есть тот самый дефект.

    ПИСАТЕЛЕЙ СКОУПА ДВА, ПОЭТОМУ ФИЛЬТР СТОИТ У ДВЕРИ, А НЕ У `arm`
    (20.08.2026). Второй писатель — легаси-пул фоновых нонсов в
    `agent.query_stream`: на НЕгейтвейном ходе он регистрирует `session.doc_ids`
    (то есть тело запроса) на каждый нонс пула. Первая редакция правки закрыла
    только `arm`, и дефект остался жив на `/query`, `/query/stream` и на
    `/chat/turns` с `pinned_tool`. Правильное место — роут: owner-гейт живёт в
    этом слое, а `orchestrator` по направлению зависимостей роуты не импортирует
    (`docs/rules/architecture.md`), так что фильтр там означал бы ВТОРОЙ
    экземпляр проверки владельца.
    """
    owned: list[str] = []
    for doc_id in doc_ids:
        doc = get_store().get_document(doc_id)
        if doc is None or not _user_can_see(doc, user):
            logging.getLogger(__name__).warning(
                "скоуп станции: документ %r не принадлежит пользователю %s — "
                "выброшен из хода", doc_id, user.id,
            )
            continue
        owned.append(doc_id)
    return owned


def _chat_turn_docs(request: QueryRequest, user: User, agent) -> list[str]:
    """Единый owner-проверенный набор документов для обеих половин хода.

    Follow-up обычно не повторяет ``doc_ids``. Тогда источник — существующая
    Session агента. Новую Session здесь не создаём: настройка транспорта не
    должна менять состояние диалога.
    """
    candidate_docs = list(request.doc_ids or [])
    if not candidate_docs and request.session_id:
        candidate_docs = agent.session_doc_ids(
            request.session_id, auth_user_id=user.id)
    if request.active_doc_id and request.active_doc_id not in candidate_docs:
        candidate_docs.append(request.active_doc_id)
    return _docs_of_the_turn(candidate_docs, user)


@app.get("/documents")
def list_documents(user: User = Depends(get_current_user)):
    """List documents owned by the caller (admins see everything)."""
    docs = get_store().list_documents(owner_id=None if user.is_admin else user.id)
    return [
        {
            "doc_id": d.doc_id,
            "filename": d.filename,
            "doc_type": d.doc_type,
            "company_name": d.company_name,
            "period": d.period,
            "language": d.language,
            "pages": d.total_pages,
            # Empty for legacy docs uploaded before this field existed.
            "ocr_engine": getattr(d, "ocr_engine", "") or "",
        }
        for d in docs
    ]


@app.get("/documents/{doc_id}/file")
def get_document_file(doc_id: str, user: User = Depends(get_current_user)):
    """Serve the original PDF file."""
    doc = get_store().get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found in index")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found in index")
    if not doc.file_path:
        raise HTTPException(status_code=404, detail="File path not recorded (document indexed without file)")
    # Файл лежит в объектном хранилище (после переезда) ИЛИ на томе (до него).
    # Решает `document_file` — одно место на весь код.
    from finengine.store.blobs import document_file  # noqa: PLC0415

    стек = ExitStack()
    try:
        path = стек.enter_context(document_file(doc.file_path))
    except FileNotFoundError:
        стек.close()
        raise HTTPException(status_code=404, detail="PDF file not found") from None
    # ⚠️ Копию убираем ПОСЛЕ отдачи ответа, а не сразу: FileResponse читает файл
    # уже за пределами этой функции. Закрой стек здесь — пользователь получил бы
    # пустой ответ, и выглядело бы это как «файл повреждён».
    background = BackgroundTask(стек.close)
    return FileResponse(
        path,
        background=background,
        media_type="application/pdf",
        filename=doc.filename,
        content_disposition_type="inline",
    )


class DocumentTypePatch(BaseModel):
    doc_type: str


@app.patch("/documents/{doc_id}")
def patch_document(
    doc_id: str, req: DocumentTypePatch, user: User = Depends(get_current_user)
):
    """Поправить вид документа руками (#250).

    ЗАЧЕМ. Вид определяется при загрузке автоматически и иногда мимо: годовой
    отчёт по МСФО приезжает как «прочее», и дальше по нему выбирается не та
    ячейка реестра. До этой ручки исправить было нечем — на экране стояла
    НАДПИСЬ, а не выбор.

    Меняем ТОЛЬКО вид: имя компании, период и путь к файлу правит извлечение,
    и перетереть их формой было бы тихой потерей данных.
    """
    from finengine.models.document import DocType

    doc = get_store().get_document(doc_id)
    if not doc or not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        new_type = DocType(req.doc_type)
    except ValueError:
        allowed = ", ".join(t.value for t in DocType)
        raise HTTPException(
            status_code=422, detail=f"Unknown doc_type «{req.doc_type}»; allowed: {allowed}"
        ) from None

    doc.doc_type = new_type
    get_store().update_document_meta(doc)
    return {"doc_id": doc_id, "doc_type": new_type.value}


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str, user: User = Depends(get_current_user)):
    """Remove a document from the store."""
    doc = get_store().get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete file — объект в хранилище ИЛИ файл на томе, смотря откуда он.
    if doc.file_path:
        from finengine.store.blobs import get_blob_store  # noqa: PLC0415

        ключ = doc.file_path.lstrip("/")
        хранилище = get_blob_store()
        if ".." not in Path(ключ).parts and хранилище.exists(ключ):
            хранилище.delete(ключ)
        else:
            Path(doc.file_path).unlink(missing_ok=True)

    get_store().remove_document(doc_id)
    return {"status": "deleted", "doc_id": doc_id}


def _set_trace_ctx(user: User, doc_id: str | None, kind: str) -> None:
    """Stamp the request-scoped ContextVar so every nested LLM call
    auto-logs to data/llm_traces/<user>/<key>.jsonl. Doc-bound paths
    use _doc_<id> as the bucket key since they don't carry project_id."""
    from finengine.llm import traces as _traces
    _traces.trace_ctx.set(_traces.TraceCtx(
        user_id=user.id,
        project_id=(f"_doc_{doc_id}" if doc_id else "default"),
        session_id=None,
        kind=kind,
    ))
@app.get("/extract/runs/{run_id}/stream")
async def extract_run_resume(
    run_id: str,
    user: User = Depends(get_current_user),
):
    """Re-subscribe to a previously-started extract run.

    The backend keeps the worker alive independently of the original
    request — page reload / VPN drop cancels the streaming response but
    not the task. This endpoint replays the on-disk journal then tails
    live events until the run finishes.

    Auth: only the user who started the run (per its meta) sees its
    events. Admins see everything.
    """
    from finengine.skills import run_orchestrator
    if run_orchestrator.чужой_прогон(run_id, user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_gen() -> AsyncGenerator[bytes, None]:
        async for ev in run_orchestrator.subscribe(run_id, kind="extract"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def _event_cursor(request: Request) -> int:
    """Read the standard SSE replay cursor, rejecting ambiguous values."""
    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        cursor = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID/after must be an integer") from exc
    if cursor < 0:
        raise HTTPException(status_code=400, detail="event cursor must be non-negative")
    return cursor


def _typed_run_response(run_id: str, *, kind: str, after_sequence: int) -> StreamingResponse:
    """A11 wire format layered over the durable legacy journal.

    The old ``/stream`` endpoints stay available for stored runs and old
    clients. New clients use ``/events`` and receive one discriminated,
    versioned envelope per durable journal entry. No prose is parsed to
    recover a result during migration.
    """
    from finengine.skills import run_orchestrator

    async def event_gen() -> AsyncGenerator[bytes, None]:
        async for event in run_orchestrator.subscribe_versioned(
            run_id,
            kind=kind,
            after_sequence=after_sequence,
            heartbeat_sec=(run_orchestrator.HEARTBEAT_SEC if kind == "chat" else None),
        ):
            event_id = "" if event.event_type == "heartbeat" else f"id: {event.sequence}\n"
            yield (
                f"{event_id}event: run_event.v1\n"
                f"data: {event.model_dump_json()}\n\n"
            ).encode("utf-8")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "X-Result-Event-Version": "1.0",
        },
    )


@app.get("/extract/runs/{run_id}/events")
async def extract_run_events(
    run_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Versioned extract result/progress events with replay cursor support."""
    from finengine.skills import run_orchestrator
    if run_orchestrator.чужой_прогон(run_id, user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")
    return _typed_run_response(run_id, kind="extract", after_sequence=_event_cursor(request))


# ── #84 C3-1: асинхронный контракт чата ────────────────────────────────
# Ход = фоновый прогон (run_orchestrator kind="chat"): POST отдаёт run_id
# сразу, ответ агент дописывает независимо от браузера; фронт подписывается
# (реплей+хвост) и ПЕРЕЖИВАЕТ перезагрузку страницы. Старый /query/stream
# остаётся как запасной путь (откат = переключение фронта).


@app.post("/chat/turns")
async def chat_turn_start(request: QueryRequest, user: User = Depends(get_current_user)):
    """Начать ход чата фоновым прогоном. Возвращает {run_id} немедленно."""
    agent = get_agent()
    owner_id = None if user.is_admin else user.id
    from finengine.skills import run_orchestrator
    from finengine.skills.run_logger import log_chat_turn
    import time as _t

    started_at = _t.time()
    cap: dict = {"session_id": request.session_id or "", "text": [], "error": None}

    # ОДИН ФИЛЬТР ВЛАДЕЛЬЦА НА ВЕСЬ ХОД, И СЧИТАЕТСЯ ОН ЗДЕСЬ, А НЕ В ВЕТКЕ
    # ГЕЙТВЕЯ. Скоуп нонса набивает не только `station_turn.arm`: на
    # НЕгейтвейном ходе (`pinned_tool`, любой `arch`, кроме `aifa_gateway`)
    # документы регистрирует легаси-пул фоновых нонсов внутри
    # `agent.query_stream` — прямо из `session.doc_ids`, то есть из тела
    # запроса. Отфильтруй мы только `arm`, вторая дверь осталась бы открытой.
    # Заодно снимается расхождение половин хода: состав сессии и скоуп станции
    # теперь описывают ОДИН набор документов.
    # Клиент передаёт `doc_ids` при выборе документа, но короткий follow-up
    # (особенно ответ на `goal_needs_clarification`) обычно несёт только
    # `session_id` и реплику. Агент на таком ходу продолжает использовать
    # документы Session; nonce-скоуп обязан строиться из того же набора, иначе
    # после успешного OCR станция закономерно получает 403 по пустому пропуску.
    _turn_docs = _chat_turn_docs(request, user, agent)
    _owned = set(_turn_docs)
    # Один набор для обеих половин хода: Session/evidence-gate и nonce-скоупа
    # станции. `request.doc_ids` здесь снова рассинхронизировал бы сохранённый
    # или только активный документ.
    _owned_doc_ids = list(_turn_docs)
    _owned_active = request.active_doc_id if request.active_doc_id in _owned else ""

    # #98 C3-2: гейтвей-петля хода. Явный эксперимент клиента (arch=aifa_gateway)
    # или глобальный флаг окружения. Кнопки с pinned_tool — всегда инлайн
    # (детерминированный прямой запуск тула, модель не участвует).
    _gw_env = os.environ.get("FINENGINE_CHAT_GATEWAY", "").strip().lower() in ("1", "true", "yes")
    use_gateway_loop = (request.arch == "aifa_gateway" or _gw_env) and not request.pinned_tool
    gw_key = f"agent:aifa-station:openresponses:fe-{uuid.uuid4().hex}" if use_gateway_loop else ""
    from finengine.api import station_turn  # #190/К5+К8b: канал станции на ход
    run_nonce = station_turn.run_nonce_of(gw_key)
    _run_cell: dict = {}

    def _runner():
        # ИМЯ ГОВОРЯЩЕЕ, И ЭТО НЕ КОСМЕТИКА (#190/К8b). Функция звалась `gen`, и
        # в agent.py есть свой `gen` — сторож «сломанный реестр не смеет
        # проглатываться» строит цепочку вызовов ПО ГОЛОМУ ИМЕНИ и склеил их в
        # одну. Через склейку он объявил нарушителями шесть чужих мест, включая
        # планировщик сценариев. Однофамилец в общем имени — это не мелочь: он
        # делает вывод сторожа неверным сразу в обе стороны.
        async def _turn_events():
            try:
                # Тумблер «Поиск в интернете» — task-local, до первого вызова
                # модели (см. openclaw.set_web_search).
                from finengine.llm.openclaw import set_web_search
                set_web_search(request.web_search)
                if gw_key:
                    # Маппинг ключ→владелец ДО первого вызова тула; session_id
                    # дорегистрируем ниже, когда агент его сообщит.
                    from finengine.api.internal_tools import register_session
                    register_session(
                        gw_key, user_id=user.id,
                        project_id=request.project_id or "",
                        run_id=_run_cell.get("run_id", ""),
                        session_id=request.session_id or "",
                    )
                    station_turn.arm(
                        nonce=run_nonce, gw_key=gw_key,
                        doc_ids=_turn_docs,
                        owner_id=user.id,
                        project_id=request.project_id or "",
                        cell=_run_cell,
                    )
                    # ВТОРОЙ КАНАЛ — АНАЛИЗ (#244, шаг 2). Свой нонс, свой
                    # адресат: у извлечения итог — таблица роли и Excel, у
                    # разбора — карточка и артефакт. На одном нонсе дренаж
                    # вернулся бы на ПЕРВОМ `done`, и второй результат ушёл бы в
                    # никуда. Взводим ВСЕГДА, как и первый: холостая подписка
                    # стоит копейки, а её отсутствие стоит целого разбора.
                    station_turn.arm(
                        nonce=station_turn.analysis_nonce_of(run_nonce),
                        gw_key=gw_key, doc_ids=_turn_docs, owner_id=user.id,
                        project_id=request.project_id or "",
                        cell=_run_cell, kind=station_turn.KIND_ANALYZE,
                    )
                    station_turn.bind_session(_run_cell, request.session_id or "")
                if request.arch == "aifa_baseline":
                    # Паритет с /query/stream: пилотный дисклеймер.
                    disclaimer = (
                        "> ⚠️ **Пилотный режим.** Автоматическая верификация ответов "
                        "настраивается — пожалуйста, критически перепроверяйте "
                        "ключевые показатели и выводы перед использованием.\n\n"
                    )
                    cap["text"].append(disclaimer)
                    yield {"type": "text_delta", "text": disclaimer}
                async for event in agent.query_stream(
                    message=request.message,
                    session_id=request.session_id,
                    doc_ids=_owned_doc_ids or None,
                    owner_id=owner_id,
                    project_id=request.project_id or "",
                    auth_user_id=user.id,
                    role=request.role or "",
                    pinned_tool=request.pinned_tool or "",
                    pinned_tool_args=request.pinned_tool_args or None,
                    gateway_session_key=gw_key,
                    active_doc_id=_owned_active,
                ):
                    t = event.get("type")
                    if t != "text_delta":
                        # #190/К8b: ГЛУШИЛКА ОДНОРАЗОВАЯ, А НЕ НА ВЕСЬ ХОД.
                        #
                        # Раньше пересказ гасился безусловно: после канона
                        # съедался КАЖДЫЙ следующий text_delta до конца хода. На
                        # туловом пути это было безобидно — тул ход завершал. В
                        # петле гейтвея ход продолжается, и ответ на второй
                        # вопрос пользователя уходил бы в никуда, неотличимо от
                        # «модель промолчала».
                        #
                        # Границей блока служит ЛЮБОЕ нетекстовое событие: новый
                        # вызов тула или шаг плана означают, что ход двинулся
                        # дальше, и следующий текст — уже ответ, а не пересказ.
                        cap["_mute"] = False
                    if t == "session_id":
                        cap["session_id"] = event.get("session_id", cap["session_id"])
                        if gw_key:
                            # Дорегистрация: тулам нужна сессия для истории.
                            from finengine.api.internal_tools import register_session
                            register_session(
                                gw_key, user_id=user.id,
                                project_id=request.project_id or "",
                                run_id=_run_cell.get("run_id", ""),
                                session_id=cap["session_id"],
                            )
                            station_turn.bind_session(_run_cell, cap["session_id"])
                    elif t == "text_delta":
                        if gw_key:
                            # Verbatim-мост: канон уже эмитнут в ран напрямую
                            # из /internal/tools/call — пересказ модели глушим
                            # (в кап канон попадает один раз, для аудит-лога).
                            from finengine.api.internal_tools import verbatim_text_of
                            _vt = verbatim_text_of(gw_key)
                            if _vt:
                                if not cap.get("_vflushed"):
                                    cap["text"].append(_vt)
                                    cap["_vflushed"] = True
                                    cap["_mute"] = True
                                if cap.get("_mute"):
                                    # Съеденное копим отдельно: глушилку,
                                    # которую нельзя проверить, однажды
                                    # начнут обходить, и «модель промолчала»
                                    # станет неотличимо от «мы съели».
                                    cap.setdefault("_muted", []).append(
                                        event.get("text", ""))
                                    continue
                        cap["text"].append(event.get("text", ""))
                    elif t == "error":
                        cap["error"] = str(event.get("error") or "")
                    yield event
            finally:
                station_turn.disarm(_run_cell)
                log_chat_turn(
                    session_id=cap["session_id"], user_id=user.id,
                    username=user.username, doc_ids=request.doc_ids or [],
                    message=request.message, response="".join(cap["text"]),
                    started_at=started_at, arch=request.arch, error=cap["error"],
                    project_id=request.project_id or "",
                )
                # Дорегистрация session_id в серверном списке чатов: на старте
                # хода сессии могло ещё не быть (первый ход нового чата).
                _upsert_dialog_stub(
                    user.id, request.project_id or "", request.message,
                    cap["session_id"], request.doc_ids or [],
                )
        return _turn_events()

    # Сервер — источник истины списка чатов: ход регистрирует чат ДО запуска,
    # чтобы даже упавший прогон оставил след (см. _upsert_dialog_stub).
    _upsert_dialog_stub(
        user.id, request.project_id or "", request.message,
        request.session_id or "", request.doc_ids or [],
    )
    run_id = run_orchestrator.start_run(
        _runner, kind="chat",
        meta={"user_id": user.id, "project_id": request.project_id or "",
              "session_id": request.session_id or ""},
    )
    # Исходное сообщение и документы хода — для инструментов ouroboros (CONTRACTS.md §3.0): цель
    # `aifa_goal` берётся отсюда, а не из аргументов модели. Запись общая для реплик.
    from finengine.assistant.turn_record import TurnRecord, remember
    remember(TurnRecord(
        run_id=run_id, owner_id=str(user.id), chat_id=request.project_id or "",
        message=request.message, user_id=user.id,
        case_id=request.project_id or request.session_id or "",
        doc_ids=tuple(_owned_doc_ids), active_doc_id=_owned_active, role=request.role or "",
    ))
    # start_run только планирует таск — до первого await управление у нас,
    # так что gen() прочитает run_id из ячейки гарантированно заполненным.
    _run_cell["run_id"] = run_id
    return {
        "schema_version": "1.0",
        "event_schema_version": "1.0",
        "run_id": run_id,
        "events_url": f"/chat/runs/{run_id}/events",
    }


@app.get("/chat/runs/{run_id}/stream")
async def chat_run_stream(run_id: str, user: User = Depends(get_current_user)):
    """Подписка на ход чата: реплей журнала + живой хвост. Переживает
    reload/обрыв сети — прогон живёт на сервере независимо. Owner-scoped."""
    from finengine.skills import run_orchestrator
    if run_orchestrator.чужой_прогон(run_id, user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_gen() -> AsyncGenerator[bytes, None]:
        # Ход штатно молчит по 3-6 минут внутри одного тула (сбор показателей,
        # написание анализа). Без кадра-биения такую паузу не переживает
        # первый же сторож простоя — и пользователь видит «Load failed» на
        # ходе, который сервер довёл до конца. Период и его вывод —
        # run_orchestrator.HEARTBEAT_SEC.
        #
        # Р17: меряем зазор между кадрами ЗДЕСЬ, на выходе ручки. Биение
        # рождается в subscribe() каждые 15 с (проверено живой пробой между
        # процессами 31.08), а фронт всё равно ловил 60+ с тишины — значит
        # кадры пропадают МЕЖДУ рождением и записью в сокет. Зазор > 40 с —
        # предупреждение с именем прогона: по нему видно, чей это стрим и в
        # какую секунду он голодал; вместе с ватчдогом петли это разводит
        # «петля стояла» и «yield застрял в записи» по разным строкам лога.
        отдано_в = time.monotonic()
        async for ev in run_orchestrator.subscribe(
            run_id, kind="chat", heartbeat_sec=run_orchestrator.HEARTBEAT_SEC
        ):
            зазор = time.monotonic() - отдано_в
            if зазор > 40.0:
                logging.getLogger(__name__).warning(
                    "SSE чата %s: зазор между кадрами %.1f с — биение не "
                    "доехало до сокета, фронт мог объявить обрыв (Р17)",
                    run_id[:8], зазор)
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
            отдано_в = time.monotonic()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/chat/runs/{run_id}/events")
async def chat_run_events(
    run_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Versioned chat events; ``Last-Event-ID`` resumes in journal order."""
    from finengine.skills import run_orchestrator
    if run_orchestrator.чужой_прогон(run_id, user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")
    return _typed_run_response(run_id, kind="chat", after_sequence=_event_cursor(request))


@app.post("/chat/runs/{run_id}/cancel")
async def chat_run_cancel(run_id: str, user: User = Depends(get_current_user)):
    """Прервать ход чата (кнопка «Стоп»): отмена серверного прогона, подписчики
    получают терминальное событие. Owner-scoped."""
    from finengine.skills import run_orchestrator
    # «Стоп» — по общему правилу: прогон может идти у соседней реплики, и
    # отменить его отсюда всё равно можно (команда уедет владельцу).
    if run_orchestrator.владелец_прогона(run_id) is None and \
            run_orchestrator.состояние_прогона(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run_orchestrator.чужой_прогон(run_id, user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")
    # #190/К8b: «Стоп» гасит и ОТЦЕПЛЕННЫЙ прогон станции. Без этого кнопка
    # снимала бы только ход, а комната продолжала бы считать минуты и писать
    # результат в чат, из которого пользователь уже ушёл.
    from finengine.api import station_turn
    station_turn.stop(run_id)
    return {"cancelled": run_orchestrator.cancel_run(run_id)}


# Сопряжение Telegram: код → состояние. Живёт в ОБЩЕМ кэше, а не в памяти
# процесса: на двух воркерах pair и поллинг status попадали на разные реплики,
# и сопряжение молча не сходилось (ревизия 27.08). Срок ведёт кэш — отдельная
# уборка не нужна.
#
# ⚠️ Бота НЕ СУЩЕСТВУЕТ: /confirm в репозитории не зовёт никто, имя ниже —
# апстримовское. Сносить заглушку или заводить бота — решение владельца
# (записано в SESSION_STATE); пока фича видна на экране настроек, она обязана
# хотя бы не ломаться о число воркеров.
_TELEGRAM_TTL_SEC = 600
_TELEGRAM_BOT_USERNAME = "@FinengineBot"


def _tg_key(code: str) -> str:
    return f"tgpair:{code}"


@app.post("/integrations/telegram/pair")
def telegram_pair():
    from finengine.store.cache import get_cache
    code = f"{secrets.randbelow(10**6):06d}"
    get_cache().set(_tg_key(code), {"linked_user": None}, ttl_sec=_TELEGRAM_TTL_SEC)
    return {
        "code": code,
        "expires_in_sec": _TELEGRAM_TTL_SEC,
        "bot_username": _TELEGRAM_BOT_USERNAME,
    }


@app.get("/integrations/telegram/status")
def telegram_status(code: str):
    from finengine.store.cache import get_cache
    p = get_cache().get(_tg_key(code))
    if not p:
        # Кэш сам стёр запись по сроку: «нет» и «истёк» здесь одно и то же.
        return {"linked": False, "expired": True, "linked_user": None}
    return {
        "linked": p["linked_user"] is not None,
        "expired": False,
        "linked_user": p["linked_user"],
    }


@app.post("/integrations/telegram/confirm")
def telegram_confirm(code: str, user: str):
    """Called by the Telegram bot after the user sends the pair code."""
    from finengine.store.cache import get_cache
    p = get_cache().get(_tg_key(code))
    if not p:
        raise HTTPException(status_code=404, detail="Code not found")
    p["linked_user"] = user
    get_cache().set(_tg_key(code), p, ttl_sec=_TELEGRAM_TTL_SEC)
    return {"status": "linked", "linked_user": user}


@app.post("/integrations/telegram/unlink")
def telegram_unlink(code: str):
    from finengine.store.cache import get_cache
    get_cache().delete(_tg_key(code))
    return {"status": "unlinked"}


class WebSearchRequest(BaseModel):
    query: str
    max_results: int = 5


@app.post("/web_search")
async def web_search_endpoint(
    body: WebSearchRequest,
    user: User = Depends(get_current_user),
):
    """Tavily-backed web search used by Finengine DEV when the user wants
    to look something up beyond the document. Returns 5 hits + the query.
    Empty results + a friendly message if the Tavily key isn't configured.
    """
    from finengine.skills.web_search import is_enabled, web_search

    if not is_enabled():
        return {
            "enabled": False,
            "results": [],
            "message": (
                "Поиск в сети не настроен на этом инстансе. "
                "Заведи API-ключ Tavily и пропиши FINENGINE_TAVILY_API_KEY."
            ),
        }
    try:
        hits = await web_search(body.query, max_results=body.max_results)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Web search failed: {e}")
    return {
        "enabled": True,
        "results": [
            {"title": h.title, "url": h.url, "content": h.content, "score": h.score}
            for h in hits
        ],
    }


@app.post("/query")
async def query(request: QueryRequest, user: User = Depends(get_current_user)):
    """Query the agent (non-streaming). Owner-scoped document access."""
    agent = get_agent()
    owner_id = None if user.is_admin else user.id
    result = agent.query(
        message=request.message,
        session_id=request.session_id,
        doc_ids=_docs_of_the_turn(request.doc_ids or [], user) or None,
        owner_id=owner_id,
        auth_user_id=user.id,
    )
    return QueryResponse(
        session_id=result["session_id"],
        answer=result["answer"],
        sources=result["sources"],
    )


@app.post("/query/stream")
async def query_stream(request: QueryRequest, user: User = Depends(get_current_user)):
    """Query the agent with streaming response. Owner-scoped document access."""
    agent = get_agent()
    # owner_id: filters documents by owner. Admin = None to see all legacy docs.
    # auth_user_id: identity for tools (watchlist, sources, traces) — must
    # always be the real user, otherwise our per-user storage layer breaks.
    owner_id = None if user.is_admin else user.id
    auth_user_id = user.id
    # Та же дверь, что и у `/chat/turns`: список уезжает в `session.doc_ids`, а
    # оттуда — в скоуп нонсов фонового пула (`agent.query_stream`). Фильтруем
    # ДО агента, потому что owner-гейт живёт здесь, в роутах.
    owned_doc_ids = _docs_of_the_turn(request.doc_ids or [], user)

    async def event_stream() -> AsyncGenerator[bytes, None]:
        # Capture user prompt + full assistant response so we can log the
        # turn at the end. Append-only to chat_runs.jsonl; one record per
        # turn. Critical for multi-user testing — lets us audit who got
        # which answer when something goes wrong.
        from finengine.skills.run_logger import log_chat_turn
        import time as _t

        started_at = _t.time()
        full_response: list[str] = []
        captured_session_id = request.session_id or ""
        captured_error: str | None = None

        # AIFA Baseline pilot disclaimer. Neutrally worded: not exposing
        # the underlying mechanics (model knowledge / inaccuracies) and not
        # implying the other modes are 100%-verified alternatives. Just a
        # plain "pilot — please double-check" notice.
        is_aifa_baseline = request.arch == "aifa_baseline"
        if is_aifa_baseline:
            disclaimer = (
                "> ⚠️ **Пилотный режим.** Автоматическая верификация ответов "
                "настраивается — пожалуйста, критически перепроверяйте "
                "ключевые показатели и выводы перед использованием.\n\n"
            )
            full_response.append(disclaimer)
            yield f"data: {json.dumps({'type': 'text_delta', 'text': disclaimer}, ensure_ascii=False)}\n\n".encode()

        try:
            async for event in agent.query_stream(
                message=request.message,
                session_id=request.session_id,
                doc_ids=owned_doc_ids or None,
                owner_id=owner_id,
                project_id=request.project_id or "",
                auth_user_id=auth_user_id,
                role=request.role or "",
                pinned_tool=request.pinned_tool or "",
                pinned_tool_args=request.pinned_tool_args or {},
            ):
                if event.get("type") == "session_id":
                    captured_session_id = event.get("session_id", captured_session_id)
                elif event.get("type") == "text_delta":
                    full_response.append(event.get("text", ""))
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        except Exception as e:
            captured_error = f"{type(e).__name__}: {e}"
            raise
        finally:
            log_chat_turn(
                session_id=captured_session_id,
                user_id=user.id,
                username=user.username,
                doc_ids=request.doc_ids or [],
                message=request.message,
                response="".join(full_response),
                started_at=started_at,
                arch=request.arch,
                error=captured_error,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── Per-user dialog persistence ────────────────────────────────────────
# Browser localStorage holds the canonical project state today, so a cache
# wipe / new device wipes the chat history. These endpoints store a single
# JSON blob per user (the same shape the frontend already builds locally)
# so the client can fetch on login and survive resets.
#
# ХРАНИЛИЩЕ СЛИВАЮЩЕЕ, а не last-write-wins. Причина — многовкладочность:
# вкладка со вчерашним снимком (в т.ч. со СТАРЫМ бандлом, где нет клиентских
# защит) пушит свой полный список и стирала на сервере всё, чего не видела.
# Так пропадали чаты. Клиентский код это не чинит — старый бандл не обновить
# с сервера, поэтому слияние обязано жить ЗДЕСЬ:
#   - проекты объединяются по id (запись не может удалить чат);
#   - переписка каждого проекта объединяется по id сообщений;
#   - скаляры берутся у более свежей стороны (updated_at);
#   - удаление — ТОЛЬКО явным списком deleted_project_ids (клиент шлёт свои
#     надгробия), реестр удалённых живёт в блобе и гасит воскрешение.
#
# САМО ХРАНЕНИЕ переехало в finengine.store.dialogs: там строка на диалог в
# Postgres (пуш одного чата не переписывает остальные) и старое файловое
# хранилище как дефолт. Слияние и tripwire остались ЗДЕСЬ — это политика
# сохранности, а не способ записи.
_DIALOGS_MAX_BYTES = 8_000_000  # 8 MB; trips at ~thousands of long messages
_DIALOGS_LOCK = threading.Lock()  # один процесс — межпоточной блокировки достаточно
_DIALOG_TOMB_TTL_S = 30 * 24 * 3600


def _msg_key(m: dict) -> str:
    """Идентичность сообщения для union. У нормальных сообщений есть id; для
    легаси-строк без id — суррогат из роли/времени/начала текста."""
    mid = m.get("id")
    if mid:
        return str(mid)
    return f"{m.get('role')}:{m.get('created_at')}:{str(m.get('content'))[:80]}"


def _msg_payload(m: dict) -> tuple:
    """Насколько сообщение «наполнено». Сравнимо только между версиями ОДНОГО
    сообщения: у карточки извлечения растёт число строк, у ответа — текст, а
    статус из running переходит в done ровно один раз и обратно не ходит."""
    return (
        len(m.get("metrics") or []),
        len(str(m.get("content") or "")),
        0 if m.get("status") == "running" else 1,
    )


def _merge_messages(a: list, b: list) -> list:
    """Union по id. При совпадении id побеждает БОЛЕЕ НАПОЛНЕННАЯ версия.

    ГРАБЛЯ, стоившая пользователю таблицы. Раньше здесь выигрывало ПЕРВОЕ
    вхождение — то есть хранимая копия. Карточка извлечения пушится на сервер
    в момент старта прогона (`metrics: []`, `status: running`), и дозаполненная
    версия уже никогда не могла её заменить: серверный снимок навсегда
    оставался пустым. Клиент при подтяжке накрывал им живую локальную копию, и
    зона «Документ» показывала пустоту при 75 извлечённых строках.
    Правило «богаче побеждает» монотонно: слияние может только дополнить
    сообщение, но не обеднить.
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    for m in list(a) + list(b):
        if not isinstance(m, dict):
            continue
        k = _msg_key(m)
        prev = best.get(k)
        if prev is None:
            best[k] = m
            order.append(k)
        elif _msg_payload(m) > _msg_payload(prev):
            best[k] = m
    out = [best[k] for k in order]
    out.sort(key=lambda m: m.get("created_at") or 0)
    return out


def _merge_project(stored: dict, incoming: dict) -> dict:
    """Скаляры — у более свежего, переписка — union, doc_ids — union."""
    fresher, older = (
        (incoming, stored)
        if (incoming.get("updated_at") or 0) >= (stored.get("updated_at") or 0)
        else (stored, incoming)
    )
    merged = dict(older)
    merged.update(fresher)
    merged["messages"] = _merge_messages(stored.get("messages") or [], incoming.get("messages") or [])
    docs: list = []
    for d in (older.get("doc_ids") or []) + (fresher.get("doc_ids") or []):
        if d and d not in docs:
            docs.append(d)
    merged["doc_ids"] = docs
    return merged


def _merge_dialog_states(
    stored: dict, incoming: dict, deleted_ids: list[str]
) -> tuple[dict, dict]:
    """Слить входящий снимок с хранимым. Возвращает (merged, drop_report):
    drop_report говорит, ЧТО потерял бы тупой last-write-wins — это и есть
    улика против затирающего писателя (пишем её в журнал client_events)."""
    now = time.time()
    tombs = {
        k: v
        for k, v in (stored.get("deleted") or {}).items()
        if isinstance(v, (int, float)) and now - v < _DIALOG_TOMB_TTL_S
    }
    for pid in deleted_ids:
        if isinstance(pid, str) and pid:
            tombs[pid] = now

    stored_by_id = {p.get("id"): p for p in stored.get("projects") or [] if isinstance(p, dict) and p.get("id")}
    incoming_by_id = {p.get("id"): p for p in incoming.get("projects") or [] if isinstance(p, dict) and p.get("id")}

    merged_projects: list[dict] = []
    seen: set[str] = set()
    for pid, inc in incoming_by_id.items():
        if pid in tombs:
            continue
        seen.add(pid)
        st = stored_by_id.get(pid)
        merged_projects.append(_merge_project(st, inc) if st else inc)
    missing = []
    shrunk = []
    for pid, st in stored_by_id.items():
        if pid in tombs:
            continue
        if pid not in seen:
            merged_projects.append(st)
            missing.append({"id": pid, "name": str(st.get("name"))[:40]})
        else:
            got = len(incoming_by_id[pid].get("messages") or [])
            had = len(st.get("messages") or [])
            if got < had:
                shrunk.append({"id": pid, "had": had, "got": got})

    folders_by_id: dict[str, dict] = {}
    for f in (stored.get("folders") or []) + (incoming.get("folders") or []):
        if not isinstance(f, dict) or not f.get("id"):
            continue
        prev = folders_by_id.get(f["id"])
        if prev is None or (f.get("updated_at") or 0) >= (prev.get("updated_at") or 0):
            folders_by_id[f["id"]] = f

    merged = {
        "projects": merged_projects,
        "folders": list(folders_by_id.values()),
        "last_project_id": incoming.get("last_project_id") or stored.get("last_project_id"),
        "deleted": tombs,
    }
    # `touched` — диалоги, которых пуш КАСАЛСЯ. Остальные попали в merged
    # ровно такими, какими лежали в хранилище, и переписывать их строки не
    # нужно: в этом вся разница между «файл целиком» и «строка на диалог».
    return merged, {"missing": missing, "shrunk": shrunk, "touched": sorted(seen)}


def _journal_client_event(username: str, kind: str, detail: str) -> None:
    """Строка в общий журнал client_events — тем же форматом, что POST
    /client-events, чтобы смотреть одним tail'ом."""
    try:
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user": username,
            "kind": kind[:64],
            "detail": detail[:500],
            "run_id": "",
            "project_id": "",
            "url": "server",
        }
        from finengine.skills.run_logger import ЖУРНАЛ_КЛИЕНТА, _ОБЩИЙ, _записать

        _записать(ЖУРНАЛ_КЛИЕНТА, _ОБЩИЙ, row)
    except Exception:  # noqa: BLE001 — телеметрия не роняет ход
        logging.getLogger(__name__).warning("client-events: журнал недоступен", exc_info=True)


def _upsert_dialog_stub(
    user_id: int,
    project_id: str,
    name_hint: str,
    session_id: str,
    doc_ids: list[str],
) -> None:
    """Авторегистрация чата в серверном списке при ходе.

    Чат, созданный во вкладке, которая ни разу не запушила /dialogs (старый
    бандл, упавшая сеть), иначе не существует нигде, кроме localStorage, — и
    его нечем восстанавливать. Ход чата ПРОХОДИТ через сервер всегда, поэтому
    сервер сам вписывает минимальную карточку: id, имя (только при создании —
    переименования клиента не затираем), session_id, документы. Переписку не
    дублируем — её клиент поднимает из серверной сессии (/chat/history).
    """
    if not project_id:
        return
    try:
        with _DIALOGS_LOCK:
            store = get_dialog_store()
            blob = store.load(user_id)
            if project_id in (blob.get("deleted") or {}):
                return  # удалён явно — не воскрешаем
            # Чат внутри папки шлёт project_id ПАПКИ (скоуп файлов) — карточку
            # чата под id папки создавать нельзя, вышел бы фантомный «чат».
            if any(
                isinstance(f, dict) and f.get("id") == project_id
                for f in blob.get("folders") or []
            ):
                return
            now_ms = int(time.time() * 1000)
            for p in blob["projects"]:
                if isinstance(p, dict) and p.get("id") == project_id:
                    if session_id:
                        p["session_id"] = session_id
                    docs = list(p.get("doc_ids") or [])
                    for d in doc_ids:
                        if d and d not in docs:
                            docs.append(d)
                    p["doc_ids"] = docs
                    p["updated_at"] = now_ms
                    break
            else:
                blob["projects"].append(
                    {
                        "id": project_id,
                        "name": (name_hint or "Новый чат").strip()[:60] or "Новый чат",
                        "created_at": now_ms,
                        "updated_at": now_ms,
                        "doc_ids": [d for d in doc_ids if d],
                        "messages": [],
                        **({"session_id": session_id} if session_id else {}),
                    }
                )
            blob["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # Пишем ровно один диалог — тот, по которому идёт ход.
            store.save(user_id, blob, changed_ids={project_id})
    except Exception:  # noqa: BLE001
        # Раньше ловили только OSError, потому что писали в файл. Теперь за
        # спиной может быть база: недоступный Postgres не имеет права уронить
        # ход чата — карточка чата тут вспомогательная, а ответ пользователю
        # важнее её.
        logging.getLogger(__name__).warning("dialog stub upsert failed", exc_info=True)


@app.get("/dialogs")
def get_dialogs(user: User = Depends(get_current_user)):
    """Return the user's saved dialog state. Empty payload on first access."""
    return get_dialog_store().load(user.id)


@app.put("/dialogs")
def put_dialogs(
    req: Request,
    payload: dict = Body(...),
    user: User = Depends(get_current_user),
):
    """Слить снимок клиента с серверным состоянием (см. комментарий блока).

    Ответ включает счётчики слияния; saved_at ставится сервером. Пуш, который
    принёс НЕ ВСЁ, что знает сервер, фиксируется в журнале client_events вместе
    с User-Agent и меткой сборки — так ловится вкладка со старым бандлом.
    """
    deleted_ids = payload.get("deleted_project_ids") or []
    if not isinstance(deleted_ids, list):
        deleted_ids = []
    with _DIALOGS_LOCK:
        store = get_dialog_store()
        stored = store.load(user.id)
        merged, report = _merge_dialog_states(stored, payload, deleted_ids)
        body_str = json.dumps(merged, ensure_ascii=False)
        if len(body_str.encode("utf-8")) > _DIALOGS_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Dialog state too large (>8 MB). Удалите старые проекты или сообщения.",
            )
        merged["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        store.save(user.id, merged, changed_ids=report["touched"])
    if report["missing"] or report["shrunk"]:
        build = req.headers.get("x-aifa-build", "") or "СТАРЫЙ БАНДЛ (без метки)"
        ua = req.headers.get("user-agent", "")[:120]
        _journal_client_event(
            user.username,
            "dialogs_push_partial",
            f"build={build}; missing={len(report['missing'])} {report['missing'][:3]}; "
            f"shrunk={len(report['shrunk'])} {report['shrunk'][:3]}; ua={ua}",
        )
    return {
        "ok": True,
        "saved_at": merged["saved_at"],
        "projects_total": len(merged["projects"]),
        "restored_for_pusher": len(report["missing"]),
    }


@app.delete("/dialogs")
def delete_dialogs(user: User = Depends(get_current_user)):
    """Wipe the user's saved dialog state — for the 'Clear server history'
    button if/when we add one. Local localStorage is unaffected."""
    get_dialog_store().delete_all(user.id)
    return {"ok": True}


# ───────────────────────── IR agents ─────────────────────────
# IR Calendar (per `Описание IR агентов.docx`): builds a 2-week-forward
# corporate-events calendar across the analytical-coverage universe.
# Streams as SSE so the UI can render incrementally.

@app.post("/ir/calendar/stream")
async def ir_calendar_stream(
    limit: int | None = Body(None, embed=True),
    project_id: str | None = Body(None, embed=True),
    user: User = Depends(get_current_user),
):
    """SSE: streams events as the calendar scan runs. When `project_id`
    is given, the final result is persisted as an artifact under that
    project, and an `artifact` event is emitted at the end with the
    run_id so the UI can link to it.

    `limit` caps how many companies to process (smoke runs use 5–10).
    """
    from finengine.skills import ir_calendar, run_orchestrator
    from finengine.store import artifacts as _art

    async def _runner():
        """Runs as a run_orchestrator background task — INDEPENDENT of this
        request. A page reload / disconnect cancels the SSE response below but
        NOT this worker, so the scan finishes and the artifact is persisted
        regardless. The final `artifact` event is journaled, so a reattaching
        client (or the live one) still receives it."""
        events: list[dict] = []
        last_done: dict = {}
        async for ev in ir_calendar.build_calendar_stream(limit=limit):
            if ev.get("type") == "events":
                events.extend(ev.get("events", []))
            elif ev.get("type") == "done":
                last_done = ev
            yield ev
        if project_id and events:
            meta = _art.save_artifact(
                user_id=user.id,
                project_id=project_id,
                kind="ir_calendar",
                payload={"events": events, "lookahead_days": 14},
                title=f"IR Calendar — {len(events)} событий",
                summary=f"{last_done.get('total_companies', 0)} компаний, окно ±14 дней",
                params={"limit": limit},
                counts={
                    "events": len(events),
                    "companies": last_done.get("total_companies", 0),
                },
            )
            yield {"type": "artifact", "run_id": meta.run_id, "kind": meta.kind}

    run_id = run_orchestrator.start_run(
        _runner,
        kind="ir_calendar",
        meta={"user_id": user.id, "project_id": project_id, "limit": limit},
    )

    async def event_stream() -> AsyncGenerator[bytes, None]:
        # Frontend persists this run_id so a reload reattaches via
        # GET /ir/calendar/runs/{run_id}/stream — the worker keeps running.
        yield f"data: {json.dumps({'type': 'run_id', 'run_id': run_id})}\n\n".encode("utf-8")
        async for ev in run_orchestrator.subscribe(run_id, kind="ir_calendar"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/ir/calendar/runs/{run_id}/stream")
async def ir_calendar_run_resume(
    run_id: str,
    user: User = Depends(get_current_user),
):
    """Re-subscribe to a previously-started IR Calendar run after a page
    reload / disconnect. The worker runs independently of the original
    request; this replays the on-disk journal then tails live events until the
    run finishes (incl. the final `artifact` event)."""
    from finengine.skills import run_orchestrator

    if run_orchestrator.чужой_прогон(run_id, user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_stream() -> AsyncGenerator[bytes, None]:
        async for ev in run_orchestrator.subscribe(run_id, kind="ir_calendar"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/projects/{project_id}/artifacts")
def list_project_artifacts(
    project_id: str,
    user: User = Depends(get_current_user),
):
    """List artifacts (skill-run outputs) saved under this project,
    newest-first. Each entry has run_id + kind + counts + title — enough
    for the Files-pane to render without fetching full payloads."""
    from finengine.store import artifacts as _art

    metas = _art.list_artifacts(user.id, project_id)
    return {"artifacts": [m.to_dict() for m in metas]}


@app.get("/projects/{project_id}/artifacts/{run_id}")
def get_project_artifact(
    project_id: str,
    run_id: str,
    user: User = Depends(get_current_user),
):
    """Fetch full artifact (meta + payload). Payload shape depends on kind:
       - ir_calendar: {"events": [...], "lookahead_days": int}"""
    from finengine.store import artifacts as _art

    try:
        return _art.load_artifact(user.id, project_id, run_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/projects/{project_id}/artifacts/{run_id}")
def delete_project_artifact(
    project_id: str,
    run_id: str,
    user: User = Depends(get_current_user),
):
    from finengine.store import artifacts as _art

    deleted = _art.delete_artifact(user.id, project_id, run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {"ok": True}


@app.get("/ir/calendar/companies")
def ir_calendar_companies(user: User = Depends(get_current_user)):
    """Diagnostic — return the list of tracked companies for IR Calendar.
    Lets the UI render a 'will scan N companies' hint before kicking the
    long-running stream."""
    from finengine.skills import ir_calendar

    try:
        companies = ir_calendar.load_companies()
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "count": len(companies),
        "companies": [
            {"region": c.region, "company": c.company, "sub_group": c.sub_group, "ir_url": c.ir_url}
            for c in companies
        ],
    }


# ───────────────────── Project Sources ─────────────────────
# Per Пакет E.2 / Пакет B.2: each project owns a `sources/` folder
# under data/users/{user_id}/projects/{project_id}/. The agent can
# read files from there via the read_source / list_sources tools
# (Пакет E.3). Frontend lets the user upload xlsx/pdf/txt/csv.

@app.post("/projects/{project_id}/sources/upload")
async def upload_source(
    project_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload a single file as a source of the given project."""
    from finengine.skills import sources as _sources

    content = await file.read()
    try:
        meta = await _sources.save_upload(
            user_id=user.id,
            project_id=project_id,
            filename=file.filename or "unnamed",
            content=content,
            content_type=file.content_type or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return meta.to_dict()


@app.get("/projects/{project_id}/sources")
def list_project_sources(
    project_id: str,
    user: User = Depends(get_current_user),
):
    """List all sources of the given project (newest first)."""
    from finengine.skills import sources as _sources

    items = _sources.list_sources(user_id=user.id, project_id=project_id)
    return {"sources": [m.to_dict() for m in items]}


@app.get("/projects/{project_id}/sources/{file_id}/meta")
def get_source_meta_endpoint(
    project_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
):
    from finengine.skills import sources as _sources

    meta = _sources.get_source_meta(user_id=user.id, project_id=project_id, file_id=file_id)
    if not meta:
        raise HTTPException(status_code=404, detail="source not found")
    return meta.to_dict()


@app.get("/projects/{project_id}/sources/{file_id}/file")
def download_source(
    project_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
):
    """Return original file bytes — used by the workspace pane to
    preview PDFs / Excels in the browser."""
    from finengine.skills import sources as _sources

    result = _sources.read_source_bytes(user_id=user.id, project_id=project_id, file_id=file_id)
    if not result:
        raise HTTPException(status_code=404, detail="source not found")
    content, meta = result
    # Имя файла в заголовок — ТОЛЬКО через RFC 5987. Заголовки кодируются в
    # latin-1, и кириллическое имя («метрики_Совкомбанк.xlsx») роняло ручку с
    # 500: браузер сохранял тело ошибки под именем .xlsx, а Excel ругался на
    # «недопустимый формат» — при том что файл на диске был цел.
    from urllib.parse import quote
    ascii_fname = re.sub(r"[^\x20-\x7e]+", "_", meta.filename) or "file"
    disposition = (
        f'inline; filename="{ascii_fname}"; ' + f"filename*=UTF-8''{quote(meta.filename)}"
    )
    return Response(
        content=content,
        media_type=meta.content_type,
        headers={"Content-Disposition": disposition},
    )


@app.get("/projects/{project_id}/sources/{file_id}/text")
def get_source_text(
    project_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
):
    """Return extracted text content (after upload-time extraction).
    Used by agent tools (read_source) and possibly UI text preview."""
    from finengine.skills import sources as _sources

    text = _sources.read_source_text(user_id=user.id, project_id=project_id, file_id=file_id)
    if text is None:
        raise HTTPException(status_code=404, detail="source text not available (binary or extraction failed)")
    return {"text": text}


@app.get("/projects/{project_id}/sources/{file_id}/sheet")
def get_source_sheet(
    project_id: str,
    file_id: str,
    sheet: str = "",
    max_rows: int = sheet_view.MAX_ROWS,
    max_cols: int = sheet_view.MAX_COLS,
    user: User = Depends(get_current_user),
):
    """Содержимое одного листа xlsx-источника — сеткой в рабочей области.

    `sheet` пустой = первый лист; имена всех листов приезжают в ответе
    (`sheets`), по ним рисуются вкладки.

    Скоуп владельца тот же, что у соседних /sources-ручек: read_source_bytes
    ходит только в data/users/{user.id}/..., чужой file_id даёт 404.
    """
    from finengine.skills import sources as _sources

    result = _sources.read_source_bytes(user_id=user.id, project_id=project_id, file_id=file_id)
    if not result:
        raise HTTPException(status_code=404, detail="source not found")
    content, meta = result

    ext = Path(meta.filename).suffix.lower()
    if ext not in sheet_view.GRID_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"файл {ext or 'без расширения'} нельзя показать сеткой: "
                "поддерживается только .xlsx/.xlsm"
                + (" — пересохраните .xls в .xlsx" if ext == ".xls" else "")
            ),
        )
    if len(content) > sheet_view.MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"файл слишком велик для просмотра сеткой: {len(content)} б "
                f"> {sheet_view.MAX_FILE_BYTES} б — скачайте его"
            ),
        )

    try:
        grid = sheet_view.read_sheet(content, sheet=sheet, max_rows=max_rows, max_cols=max_cols)
    except sheet_view.SheetNotFound:
        raise HTTPException(status_code=404, detail=f"листа {sheet!r} нет в книге")
    except sheet_view.SheetReadError as e:
        raise HTTPException(status_code=422, detail=f"не удалось прочитать xlsx: {e}")

    return {"file_id": file_id, "filename": meta.filename, **grid}


@app.delete("/projects/{project_id}/sources/{file_id}")
def delete_source_endpoint(
    project_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
):
    from finengine.skills import sources as _sources

    ok = _sources.delete_source(user_id=user.id, project_id=project_id, file_id=file_id)
    if not ok:
        raise HTTPException(status_code=404, detail="source not found")
    return {"ok": True}


class ClientEvent(BaseModel):
    """Событие клиента для диагностики (обрыв SSE, сбой рендера и т.п.)."""

    kind: str = ""
    detail: str = ""
    run_id: str = ""
    project_id: str = ""
    url: str = ""
    at: int = 0


@app.post("/client-events")
def log_client_event(ev: ClientEvent, user: User = Depends(get_current_user)) -> dict:
    """Принять событие браузера в общий журнал.

    Сбои КЛИЕНТА (оборванный SSE, уснувшая вкладка, упавший рендер) в серверных
    логах не видны вообще — диагностировать их постфактум было нечем. Пишем
    рядом с остальными журналами, по строке на событие.
    """
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user": user.username,
        "kind": ev.kind[:64],
        "detail": ev.detail[:500],
        "run_id": ev.run_id[:64],
        "project_id": ev.project_id[:64],
        "url": ev.url[:120],
    }
    try:
        from finengine.skills.run_logger import ЖУРНАЛ_КЛИЕНТА, _ОБЩИЙ, _записать

        _записать(ЖУРНАЛ_КЛИЕНТА, _ОБЩИЙ, row)
    except OSError:
        logging.getLogger(__name__).warning(
            "client-events: не удалось записать журнал", exc_info=True
        )
    return {"ok": True}


@app.get("/chat/history/{session_id}")
def get_chat_history(session_id: str, user: User = Depends(get_current_user)) -> dict:
    """История серверной сессии чата — страховочная копия переписки.

    Клиентская лента живёт в localStorage и может быть повреждена (гонки
    писателей, потерянная вкладка). Серверная сессия пишется агентом на каждом
    ходе и переживает всё это — по ней фронт восстанавливает пропавший тред.

    Скоуп владельца: сессия ищется ТОЛЬКО у `user.id`. Прежде это держалось
    каталогом (`data/sessions/<owner_id>/`), теперь — условием запроса в
    хранилище; разбор и цена ошибки — в шапке `store/sessions.py`.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        raise HTTPException(status_code=400, detail="bad session id")
    from finengine.store.sessions import get_session_store

    try:
        data = get_session_store().load(user.id, session_id)
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=500, detail="session unreadable") from None
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    out = []
    for e in (data.get("history") or [])[-200:]:
        role = e.get("role")
        if role in ("user", "assistant"):
            out.append({"role": role, "content": str(e.get("content") or "")[:20000]})
        elif role == "tool_call":
            out.append({"role": "tool_call", "name": str(e.get("name") or "")[:64]})
        # tool_output в восстановление не отдаём: это внутренности хода,
        # в ленте они никогда не показывались.
    return {"session_id": session_id, "history": out}
