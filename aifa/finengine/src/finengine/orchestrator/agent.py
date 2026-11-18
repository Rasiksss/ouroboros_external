"""FA-CLAW orchestrator. Chat path routed through OpenClaw gateway
(Kimi K2.5 + thinking max owned by gateway config).

Backend container reaches the gateway via a socat forwarder on the host:
172.18.0.1:18789 (compose-net gateway IP) → 127.0.0.1:18789 (gateway loopback).
Forwarder runs as `openclaw-bridge-forward.service` user-systemd unit.

Verified working on OpenClaw 2026.5.2 — the 2026.4.24 event-loop hang
is fixed (smoke 2026-05-05).
"""
from __future__ import annotations
import functools
import logging
import uuid
from typing import TYPE_CHECKING, AsyncGenerator

if TYPE_CHECKING:  # аннотация адресата дренажа; в рантайме модуль не нужен
    from finengine.skills.station_run_sink import StationRun

logger = logging.getLogger(__name__)

from finengine.llm import openclaw as llm  # noqa: E402
# Потолок жизни фонового воркера берём у сторожа, а не пишем числом здесь:
# сторож обязан помнить прогон дольше, чем тот живёт, и два независимых числа
# уже разъехались однажды (память 900 с против воркера 1200 с — сторож молча
# выключался ровно на длинных прогонах).
from finengine.skills.grounding_bus import WORKER_LIFETIME_SEC as _WORKER_LIFETIME_SEC  # noqa: E402
from finengine.ingestion.document_store import get_store  # noqa: E402


def _summarize_tool_args(name: str, args: dict) -> str:
    """One-line human label for a tool call — drives the chip text in the
    chat UI. Per tool we pick the most informative field; truncate to
    ~60 chars so chips stay tidy. Returns "" when nothing useful exists."""
    if not isinstance(args, dict) or not args:
        return ""
    candidates: list[str] = []
    if name == "tavily_search":
        candidates.append(str(args.get("query", "")))
    elif name == "fetch_url":
        candidates.append(str(args.get("url", "")))
    elif name == "read_source":
        candidates.append(str(args.get("file_id", ""))[:8])
    elif name == "update_watchlist":
        action = str(args.get("action", ""))
        item = str(args.get("name", ""))
        candidates.append(f"{action} {item}".strip())
    else:
        # Generic fallback — first string-typed arg up to 60 chars.
        for v in args.values():
            if isinstance(v, str) and v:
                candidates.append(v)
                break
    summary = next((c for c in candidates if c), "")
    if len(summary) > 60:
        summary = summary[:57] + "…"
    return summary


# Tools whose JSON result carries canonical, pre-rendered markdown. The
# backend streams that markdown verbatim and suppresses any further LLM
# text — otherwise the model rewrites/truncates it at its max_output
# budget (e.g. echoes ~7 of 75 metric rows).
_VERBATIM_TOOLS = ("daily_news_digest", "ir_calendar_run")


def render_extract_result(payload: dict) -> tuple[str | None, list[dict]]:
    """Канон извлечения: шапка + таблица прогона, рядом кнопка Excel.

    ⚠️ ОТРИСОВКА НАЗВАНА ПО ИСХОДУ, А НЕ ПО ТУЛУ (#166). Раньше вход был
    `tool_name`, и путь по навыкам звал её строкой `"fe_extract_metrics"` —
    то есть боевой путь адресовался ИМЕНЕМ СНЕСЁННОГО ТУЛА. Отказ такого
    вызова был бы немым: гейт `not in _VERBATIM_TOOLS` возвращает пусто, а
    приёмник это только пишет в лог. Исход — вещь, тул — способ; связывать
    отрисовку надо с первым.
    """
    table_md = payload.get("table_markdown")
    if not table_md:
        return None, []
    events: list[dict] = []
    fid = payload.get("excel_file_id")
    if fid:
        # `file` event → the UI shows a real Excel download button
        # (like the Sources panel), not a bare "📎 Excel" the user
        # can't click.
        events.append({
            "type": "file",
            "file_id": fid,
            "filename": payload.get("excel_filename") or "metrics.xlsx",
            "kind": "excel",
        })
    from finengine.roles.access import ROLE_LABELS  # noqa: PLC0415
    rl = ROLE_LABELS.get(payload.get("role") or "", payload.get("role") or "")
    aps = payload.get("all_periods_in_report") or []
    proj = [x for x in aps
            if x not in (payload.get("main_period"), payload.get("prev_period"))]
    seg = payload.get("segments_count") or 0
    grp = payload.get("group_count") or 0
    header = (
        f"## {payload.get('company_name') or 'Отчёт'}\n"
        f"**Ячейка:** {payload.get('cell', '')} · **Роль:** {rl} · "
        f"**Период:** {payload.get('main_period', '')}"
        + (f" (сравнение {payload.get('prev_period')})" if payload.get("prev_period") else "")
        + f" · **Единицы:** {payload.get('unit', '')}\n"
        f"Извлечено **{payload.get('metrics_extracted', 0)}/"
        f"{payload.get('metrics_total_for_role_cell', 0)}** показателей."
        + (f" В отчёте также периоды: {', '.join(proj)} — полностью в Excel."
           if proj else "")
        + ((f" Сегментная разбивка ({seg}) и состав группы ({grp}) — "
            "в приложенном Excel (листы «Сегменты», «Группа»).")
           if (seg or grp) else "")
    )
    return header + "\n\n" + table_md, events


def render_analysis_result(payload: dict) -> tuple[str | None, list[dict]]:
    """Канон разбора: структурная карточка фронту, markdown как запасной вид.

    Событие `analysis_card` — ЕДИНСТВЕННОЕ, по которому фронт рисует карточку
    разбора (`web/src/lib/projects.ts:1481`). Пока эта отрисовка жила веткой
    внутри `_verbatim_render` по имени `fe_analyze`, путь по навыкам её не
    доставал вовсе: он шлёт `analysis_done`, а такого имени не знают ни
    `_progress_events`, ни фронт, — и готовый разбор доезжал до экрана
    питоновским репрезентом словаря в строке прогресса.
    """
    events: list[dict] = []
    card = payload.get("report_structured")
    if card:
        events.append({"type": "analysis_card", "card": card})
    return payload.get("report_markdown") or None, events


def _verbatim_render(tool_name: str, output_full: str) -> tuple[str | None, list[dict]]:
    """Canonical render of a pre-rendered tool result.

    Returns (markdown, side_events):
      markdown    — the verbatim text to stream (None → no short-circuit;
                    the LLM's own text keeps flowing);
      side_events — client events to emit BEFORE the text.
    """
    if tool_name not in _VERBATIM_TOOLS:
        return None, []
    import json as _json  # noqa: PLC0415
    try:
        p = _json.loads(output_full or "{}")
    except _json.JSONDecodeError:
        p = {}
    if not isinstance(p, dict):
        return None, []
    # ir_calendar_run pre-renders the full events table; streaming it
    # as-is avoids the LLM truncating it / adding "уточните запрос".
    return p.get("markdown") or None, []


def _build_context(
    message: str,
    doc_ids: list[str] | None,
    owner_id: int | None = None,
) -> tuple[str, list[dict]]:
    """Полный контекст: текст каждого выбранного документа из .txt-сайдкара.

    ⚠️ ОТКАТА НА ВЕКТОРНЫЙ ПОИСК БОЛЬШЕ НЕТ (снос 15.08.2026). Раньше документ
    без сайдкара добирался фрагментами из `index.search`. Отката не стало
    вместе с поиском, и терять было нечего: файла векторного индекса не
    существует ни на проде, ни на dev — откат искал бы в пустоте и возвращал
    пустой список. Теперь такой документ ЧЕСТНО пропускается и говорит об этом
    в логе, а не молча даёт ответ без него.
    """

    index = get_store()
    if not doc_ids:
        return "", []

    parts: list[str] = []
    sources: list[dict] = []

    for doc_id in doc_ids:
        meta = index.get_document(doc_id)
        if not meta:
            continue
        if owner_id is not None and meta.owner_id and meta.owner_id != owner_id:
            continue

        from finengine.store.blobs import document_text  # noqa: PLC0415

        text = document_text(doc_id)
        if text is not None:
            # Cap per-doc text so the chat prompt can't blow the gateway's
            # request limit. A full IFRS report is ~280K chars / ~70K tokens →
            # the gateway returned an empty stream (0 tokens) → empty answer.
            # The agent does NOT need the whole text in chat: the extraction
            # skill reads the COMPLETE document inside the station. Keep a
            # generous head for chat Q&A.
            _DOC_CTX_CAP = 40_000
            if len(text) > _DOC_CTX_CAP:
                text = (text[:_DOC_CTX_CAP] + "\n\n…[документ сокращён для чата "
                        f"(полный — {len(text)} символов). Навыки работы с отчётом читают "
                        "ВЕСЬ документ в станции — не отвечай по этому обрезку, если нужен "
                        "разбор или таблица показателей.]")
            label = meta.company_name or meta.filename
            # МЕТАКАРТА (#189) — строкой под шапкой документа. До неё модель
            # знала про документ только имя файла и потому путала документы
            # между собой, когда их несколько, и запускала извлечение полного
            # реестра по пресс-релизу. Карта снята ОДИН раз при приёме; здесь
            # только показываем. Нет карты (документ принят до её появления
            # либо модель тогда не ответила) — строки просто не будет.
            from finengine.skills.meta_card import as_line as _card_line
            card = _card_line(meta.meta_card, filename=meta.filename) + "\n" \
                if meta.meta_card else ""
            # 🔴 ПРЕДУПРЕЖДЕНИЕ СТОИТ У КАЖДОГО ДОКУМЕНТА, А НЕ ТОЛЬКО У
            # ОБРЕЗАННОГО (20.08.2026). Ход по Северстали: модель не нашла, чем
            # прочитать документ тулами (`list_sources` пуст — отчёт лежит в
            # ДОКУМЕНТАХ, а не в «Источниках»), увидела текст у себя в контексте
            # и ПРОЧИТАЛА ЧИСЛА ГЛАЗАМИ: восемь показателей и четыре
            # коэффициента без единого запуска извлечения. Числа совпали с
            # разметкой — и это худший исход: ни таблицы, ни Excel, ни страницы,
            # а отличить такой ответ от честного пользователю нечем.
            #
            # Прежняя оговорка висела ВНУТРИ пометки об обрезке, то есть только
            # у длинных документов, и была советом («не отвечай по этому
            # обрезку»), а не запретом с названной заменой. Теперь это запрет,
            # он стоит у любого документа и называет, что делать вместо.
            parts.append(
                f"## Документ: {label} [doc_id: {doc_id}]\n{card}\n"
                "⚠️ ЭТОТ ТЕКСТ — ДЛЯ ФАКТОВ И ЦИТАТ, НЕ ДЛЯ ЧИСЕЛ ОТЧЁТНОСТИ.\n"
                "Показатели (выручка, прибыль, долг, потоки, коэффициенты) читать "
                "отсюда ГЛАЗАМИ и подавать в расчёт ЗАПРЕЩЕНО: у такого числа нет "
                "страницы-источника, и проверить его нельзя. Их даёт извлечение — "
                "оно читает документ целиком и возвращает таблицу со ссылками. "
                "Запустить нечем — скажи это прямо и остановись, но числа не "
                "выписывай.\n\n"
                f"{text}")
            sources.append({"filename": meta.filename, "company_name": meta.company_name})
        else:
            # Текста нет — молчать нельзя: иначе модель ответит «по документу»,
            # не увидев его ни строки, и это не отличить от честного ответа.
            logger.warning(
                "документ %s (%s) не попал в контекст: распознанного текста ещё нет",
                doc_id, meta.filename,
            )

    return "\n\n========\n\n".join(parts), sources


class Session:
    """История сессии хранит ВСЕ типы записей: user / assistant / tool_call /
    tool_output. Это нужно чтобы на follow-up'е модель видела результат
    предыдущего tool-вызова, а не пыталась перевызвать его (или галлюцинировать).

    Без tool_output в истории на «как ты провёл расчёт?» модель видела
    только текстовый ответ ассистента — без `factors[]`, `method_notes`,
    `formula` — и опять звала factor_analysis заново.
    """

    def __init__(self):
        self.session_id = str(uuid.uuid4())
        # Записи: {"role": "user"|"assistant"|"tool_call"|"tool_output", ...}
        # Поля для tool_call: name, args
        # Поля для tool_output: name, ok, output_full (raw JSON str)
        self.history: list[dict] = []
        self.doc_ids: list[str] = []
        # Владелец сессии (auth_user_id). Сессия ПРИВЯЗАНА к пользователю —
        # её нельзя загрузить из чужого контекста (закрытие cross-user IDOR).
        self.owner_id: int | None = None
        # Rolling compaction (C): старые ходы сворачиваются сюда, чтобы история
        # не росла бесконечно. См. _maybe_summarize.
        self.summary: str = ""

    def add(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def add_tool_call(self, name: str, args: dict) -> None:
        self.history.append({"role": "tool_call", "name": name, "args": args})

    def add_tool_output(self, name: str, ok: bool, output_full: str) -> None:
        self.history.append({
            "role": "tool_output", "name": name, "ok": ok,
            "output_full": output_full,
        })

    def format_history(self) -> str:
        # `query_stream` дописывает ТЕКУЩЕЕ сообщение юзера в историю ПОСЛЕ
        # сборки промпта (вопрос инжектится отдельно), поэтому на момент рендера
        # в `history` лежат только ПРОШЛЫЕ ходы — рендерим их целиком (включая
        # последний ответ ассистента, который раньше терялся из-за [:-1]).
        if not self.history and not self.summary:
            return ""
        blocks: list[str] = []
        if self.summary:
            blocks.append(f"[Резюме раннего диалога]: {self.summary}")
        body = _render_entries(self.history)
        if body:
            blocks.append(body)
        return "\n".join(blocks)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "history": self.history,
            "doc_ids": self.doc_ids,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        s = cls()
        if d.get("session_id"):
            s.session_id = d["session_id"]
        s.owner_id = d.get("owner_id")
        s.history = d.get("history") or []
        s.doc_ids = d.get("doc_ids") or []
        s.summary = d.get("summary") or ""
        return s


def _build_ingress_context(session: "Session", *, active_doc_id: str, ui_role: str):
    """Контекст для модели-маршрутизатора (`intent.parse_message`): документы,
    роль, выбранная в интерфейсе, и последние реплики диалога.

    Заменяет прежние снимки состояния уточнений (`Session.clarification`/
    `last_resolved_goal`): короткая реплика («а за прошлый год?») понятна
    модели из самой истории. Метаданные документа читаются best-effort — сбой
    хранилища не должен ронять ход, имя тогда — сам идентификатор."""
    from finengine.skills.atoms.intent import IngressContext  # noqa: PLC0415

    documents: list[tuple[str, bool]] = []
    for doc_id in session.doc_ids or []:
        name = doc_id
        try:
            meta = get_store().get_document(doc_id)
            name = (getattr(meta, "company_name", "") or getattr(meta, "filename", "") or doc_id) if meta else doc_id
        except Exception:  # noqa: BLE001 — подсказка модели, не критичный путь
            pass
        is_active = doc_id == active_doc_id or len(session.doc_ids) == 1
        documents.append((str(name), is_active))
    history = tuple(
        (entry["role"], str(entry.get("content") or ""))
        for entry in session.history
        if entry.get("role") in ("user", "assistant") and str(entry.get("content") or "").strip()
    )[-6:]
    return IngressContext(documents=tuple(documents), ui_role=ui_role or "", history=history)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…[урезано]"


# Какие поля в выводе тула «шум» (тяжёлые / повторяющиеся / редко нужны на
# follow-up). Сохраняем всё остальное.
_TOOL_NOISY_FIELDS = {
    "factor_analysis": {"parser_warnings", "products_breakdown"},
    "daily_news_digest": {"items"},  # markdown уже стримится в основной ответ
}
# Какие поля наоборот критически важны (оставляем целиком даже если длинные).
_TOOL_CRITICAL_FIELDS = {
    "factor_analysis": {"aggregates", "factors", "method_notes",
                        "xlsx_artifact", "scope"},
}


def _compact_tool_output(tool_name: str, raw_json: str, max_chars: int = 6000) -> str:
    """Сжать tool_output для истории.

    Для известных тулов парсим JSON и вырезаем шумные поля. Иначе — просто
    truncate. by_product у каждого фактора оставляем (модель часто хочет
    детализацию на follow-up'е).
    """
    if not raw_json:
        return "(empty)"
    import json as _json
    try:
        payload = _json.loads(raw_json)
    except Exception:
        return _truncate(raw_json, max_chars)
    if not isinstance(payload, dict):
        return _truncate(raw_json, max_chars)
    # Verbatim-тулы (ir_calendar_run, daily_news_digest) кладут в `markdown`
    # готовую таблицу/дайджест — ровно то, что нужно на follow-up'е, чтобы
    # ОТВЕТИТЬ ИЗ ИСТОРИИ, не перезапуская навык. Держим её целиком (до 16K).
    md = payload.get("markdown")
    if isinstance(md, str) and md.strip():
        return _truncate(md, 16000)
    noisy = _TOOL_NOISY_FIELDS.get(tool_name, set())
    for k in noisy:
        payload.pop(k, None)
    compact = _json.dumps(payload, ensure_ascii=False, default=str)
    return _truncate(compact, max_chars)


# ── Диалоговая память: рендер истории, персист на диск (B), компакт (C) ──────
def _render_entries(entries: list[dict]) -> str:
    """Рендер списка записей истории в текст для промпта/суммаризации."""
    lines: list[str] = []
    for msg in entries:
        role = msg.get("role")
        if role == "user":
            lines.append(f"Пользователь: {_truncate(msg.get('content', ''), 4000)}")
        elif role == "assistant":
            lines.append(f"Ассистент: {_truncate(msg.get('content', ''), 4000)}")
        elif role == "tool_call":
            args = msg.get("args") or {}
            arg_preview = ", ".join(
                f"{k}={_truncate(str(v), 60)}" for k, v in args.items()
            ) or "no args"
            lines.append(f"[Tool call: {msg.get('name')}({arg_preview})]")
        elif role == "tool_output":
            ok_mark = "OK" if msg.get("ok") else "ERROR"
            compact = _compact_tool_output(msg.get("name", ""), msg.get("output_full") or "")
            lines.append(f"[Tool result: {msg.get('name')} ({ok_mark})]\n{compact}")
    return "\n".join(lines)


def _save_session(session: "Session") -> None:
    """Персист истории — переживает рестарт и деплой бэкенда.

    Пишется под владельцем сессии: он же отделяет её от чужих (см. шапку
    `store/sessions.py` — граница при переезде из каталогов стала условием
    запроса, и это самое опасное место переезда).
    """
    from finengine.store.sessions import get_session_store  # noqa: PLC0415

    try:
        get_session_store().save(session.owner_id, session.session_id, session.to_dict())
    except Exception as e:  # noqa: BLE001
        logger.warning("session save failed: %s", e)


async def _bg_run_relay(nonce: str, q, timeout: float = _WORKER_LIFETIME_SEC):
    """#84 фронт-подводка: релей СЫРЫХ событий станции (start/metric/done/error)
    в run_orchestrator-run kind="extract" — фронт подписывается существующим
    GET /extract/runs/{run_id}/stream и рисует живую таблицу (applyExtractEvent
    ест ровно эти типы). Вторая очередь того же нонса (fan-out ingest); mailbox
    работает параллельно на своей. Если спавна не было (модель пошла синхронно),
    таймаут гасит run МОЛЧА (без событий фронт ничего не рисует — нет мусора)."""
    import asyncio
    from finengine.api import station_progress_ingest as _ingest
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    saw_any = False
    try:
        while True:
            left = deadline - loop.time()
            if left <= 0:
                if saw_any:
                    yield {"type": "error", "message": "фоновый прогон затих (таймаут)"}
                return
            try:
                ev = await asyncio.wait_for(q.get(), timeout=left)
            except asyncio.TimeoutError:
                if saw_any:
                    yield {"type": "error", "message": "фоновый прогон затих (таймаут)"}
                return
            if not isinstance(ev, dict):
                continue
            saw_any = True
            yield ev
            if ev.get("type") in ("done", "error"):
                return
    finally:
        _ingest.unregister(nonce, q)


async def _bg_report_done(session: "Session", project_id: str,
                          run: "StationRun") -> None:
    """Доклад воркера в ИСТОРИЮ СЕССИИ: следующий ход модели её увидит.

    #190/К8b: сборка книги переехала СЮДА из дренажа. Книга у двух адресатов
    разная — воркеру нужна плоская выгрузка всех периодов, ходу роль-форматная
    из общего рецепта (`extract_result.finalize_extraction`). Пока она собиралась
    в дренаже, у хода получилось бы либо две книги, либо чужой формат.
    """
    import json  # noqa: PLC0415

    from finengine.skills import extract_result  # noqa: PLC0415

    summary = dict(run.summary)
    if run.metrics:
        try:
            # Паспорт правил — и в фоновой выгрузке тоже (#155). Файл у
            # пользователя такой же, как из инлайн-пути, и без признака версии
            # реестра он ничем не хуже и не лучше вчерашнего: сравнить их будет
            # нечем. Ячейку сообщает сама станция в `done` — своей копии
            # классификации тут нет.
            passport = extract_result.passport_rows(
                extract_result.registry_passport(str(summary.get("cell") or "")))
            content = extract_result.build_metrics_workbook(
                run.metrics, run.segment_rows, run.group_rows, passport=passport)
            fname = extract_result.metrics_xlsx_filename(
                str(summary.get("company_name") or "report"))
            file_id = await extract_result.save_metrics_xlsx(
                content=content, filename=fname,
                user_id=session.owner_id or 0, project_id=project_id or "default")
            # ОБА поля — ПОСЛЕ сохранения. Здесь это осознанно иначе, чем у
            # тула: там имя книги переживает отказ хранилища (пользователю
            # говорят, какой файл не сохранился), а тут сводка уходит В ИСТОРИЮ
            # и попадёт в промпт следующего хода — имя несуществующего файла
            # модель предложит скачать.
            summary["excel_file_id"] = file_id
            summary["excel_filename"] = fname
        except Exception:  # noqa: BLE001 — сводка важнее файла
            logger.warning("bg mailbox xlsx save failed doc=%s",
                           run.doc_id[:8], exc_info=True)
    session.add_tool_output("background_extract", True, json.dumps(
        {"status": "завершено", **summary}, ensure_ascii=False))
    _save_session(session)


def _bg_report_failed(session: "Session", message: str) -> None:
    """То же для явного отказа. Молчание воркера сюда НЕ попадает — оно
    помечается только в шине состояния (см. skills/station_run_sink)."""
    import json  # noqa: PLC0415
    session.add_tool_output("background_extract", False, json.dumps(
        {"status": "ошибка", "message": message}, ensure_ascii=False))
    _save_session(session)


async def _bg_extract_mailbox(session: "Session", nonce: str, q,
                              timeout: float = _WORKER_LIFETIME_SEC,
                              project_id: str = "default") -> None:
    """#84 Фаза C: почтовый ящик фонового извлечения (воркер `sessions_spawn`).

    Бэк ВЫДАЁТ нонс оркестратору (промпт-секция «Фоновое извлечение») и слушает
    релей #83 под этим нонсом. Когда движок в комнате воркера завершится, ловим
    `done` и кладём сводку в историю сессии как [Tool result: background_extract]
    — следующий ход модели видит её и пересказывает юзеру. Гейтвей-announce для
    нашего /v1/responses невидим (каждый ход строится из запроса бэка, не из
    транскрипта гейтвей-сессии) — поэтому подхват бэковый, на детерминированном
    нонсе (решённый паттерн корреляции «нонс через аргументы»).

    #190/К2: сам дренаж очереди переехал в `skills/station_run_sink.drain` —
    его же будет звать комната ХОДА, у которой другой адресат доклада. Здесь
    остаётся ровно то, что у воркера СВОЁ: адресат (история сессии) и владение
    очередью нонса.

    ⚠️ `on_event` здесь НЕ задан, и это осознанно: воркер работает в фоне, его
    результат нужен следующему ходу, а живой таблицы у него на экране нет —
    рисовать построчно некуда.
    """
    from finengine.api import station_progress_ingest as _ingest  # noqa: PLC0415
    from finengine.skills import station_run_sink  # noqa: PLC0415
    # Очереди нет — снимать регистрацию НЕЛЬЗЯ: `unregister(nonce, None)`
    # сбрасывает ВСЕ очереди нонса, включая чужую очередь фронт-подводки.
    if q is None:
        return
    try:
        await station_run_sink.drain(
            nonce=nonce, queue=q, owner_id=session.owner_id, timeout=timeout,
            on_done=functools.partial(_bg_report_done, session, project_id or "default"),
            on_failed=functools.partial(_bg_report_failed, session),
        )
    except Exception:  # noqa: BLE001
        logger.warning("bg mailbox failed nonce=%s", nonce, exc_info=True)
    finally:
        _ingest.unregister(nonce, q)


def _load_session(session_id: str, owner_id: int | None = None):
    """Грузит сессию ТОЛЬКО у переданного владельца. Чужую не отдаёт."""
    from finengine.store.sessions import get_session_store  # noqa: PLC0415

    try:
        данные = get_session_store().load(owner_id, session_id)
        return Session.from_dict(данные) if данные is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning("session load failed: %s", e)
        return None


# Rolling compaction (C): когда история переросла триггер, сворачиваем самые
# старые ходы в `summary` и оставляем «хвост» дословно. Триггер высокий, чтобы
# обычные короткие диалоги его не задевали.
_SUMMARY_TRIGGER = 28   # записей истории (~10-14 ходов)
_KEEP_RECENT = 10       # сколько последних записей оставляем дословно


async def _maybe_summarize(session: "Session") -> bool:
    """Свернуть старые ходы в session.summary. Best-effort: при любой ошибке
    история остаётся целой. Возвращает True, если реально сжали."""
    if len(session.history) <= _SUMMARY_TRIGGER:
        return False
    fold = session.history[:-_KEEP_RECENT]
    keep = session.history[-_KEEP_RECENT:]
    if not fold:
        return False
    prev = f"Прошлое резюме:\n{session.summary}\n\n" if session.summary else ""
    prompt = (
        f"{prev}Сожми историю диалога в плотное резюме на русском. ОБЯЗАТЕЛЬНО "
        "сохрани конкретику: какие навыки уже запускались и их РЕЗУЛЬТАТЫ "
        "(собранные таблицы/числа/даты/тикеры/артефакты), выбор и решения "
        "пользователя, открытые вопросы. Без воды, без вступлений.\n\n"
        f"=== История ===\n{_truncate(_render_entries(fold), 24000)}"
    )
    try:
        summary = await llm.complete(prompt, max_output=1200)
    except Exception as e:  # noqa: BLE001
        logger.warning("dialog summarize failed (kept full history): %s", e)
        return False
    if not summary or not summary.strip():
        return False
    session.summary = summary.strip()
    session.history = keep
    return True


def _own_definitions_for(doc_ids: list[str] | None, user_id: object) -> str:
    """Свои показатели и формулы, заведённые РАНЬШЕ по этим документам.

    Пустая строка — заводить было нечего, и это самый частый ответ: блок
    появляется только там, где человек уже просил показатель или коэффициент,
    которых нет в реестре.

    ⚠️ ПОСЛЕДНЯЯ СЕТЬ ЗДЕСЬ ОБЯЗАТЕЛЬНА. Хранилище исходов может быть выключено
    (на стенде без `FINENGINE_RUNS_DATABASE_URL` оно молчит всегда), и падать
    из-за отсутствующей ПОДСКАЗКИ — значит ронять ход ради удобства.
    """
    if not doc_ids:
        return ""
    try:
        from finengine.store.runs import get_run_store

        store = get_run_store()
        показатели: dict[str, dict] = {}
        формулы: dict[str, dict] = {}
        for doc_id in list(doc_ids)[:8]:
            outcome = store.latest_for_doc(str(doc_id), user_id=int(user_id or 0),
                                           kind="extract")
            payload = (outcome.payload if outcome else None) or {}
            for row in payload.get("own_metrics") or []:
                имя = str((row or {}).get("enum") or "").strip().upper()
                if имя:
                    показатели[имя] = row
            for row in payload.get("own_calculated") or []:
                имя = str((row or {}).get("enum") or "").strip().upper()
                if имя:
                    формулы[имя] = row
    except Exception:  # noqa: BLE001 — подсказка не смеет уронить ход
        logger.warning("свои определения не прочитаны", exc_info=True)
        return ""

    if not показатели and not формулы:
        return ""
    строки = ["## Твои прежние определения по этим документам",
              "Ты уже заводила их сама. Нужна правка — бери ЭТУ запись и меняй "
              "в ней одно поле, а не сочиняй заново."]
    for имя, row in показатели.items():
        подсказка = str(row.get("hint") or "")[:160]
        строки.append(f"* показатель `{имя}` — {row.get('name_ru') or ''}"
                      + (f"; где искать: {подсказка}" if подсказка else ""))
    for имя, row in формулы.items():
        строки.append(f"* формула `{имя}` — {row.get('name_ru') or ''}: "
                      f"`{row.get('formula') or ''}`")
    return "\n".join(строки)


class FinengineAgent:
    def __init__(self):
        self.sessions: dict[str, Session] = {}

    def session_doc_ids(self, session_id: str = "",
                        auth_user_id: int | None = None) -> list[str]:
        """Документы существующей owner-scoped Session без её создания.

        Это read-only lookup для транспорта до старта ``query_stream``.
        Неизвестный или чужой ``session_id`` не создаёт Session-замену: иначе
        API и агент вошли бы в один ход с двумя разными identity сессии.
        """
        if not session_id:
            return []
        cached = self.sessions.get(session_id)
        if cached is not None and cached.owner_id == auth_user_id:
            return list(cached.doc_ids)
        loaded = _load_session(session_id, auth_user_id)
        if loaded is None or loaded.owner_id != auth_user_id:
            return []
        self.sessions[loaded.session_id] = loaded
        return list(loaded.doc_ids)

    def get_or_create_session(self, session_id: str = "", auth_user_id: int | None = None) -> Session:
        # Сессия ПРИВЯЗАНА к владельцу (auth_user_id). client-supplied session_id
        # от ДРУГОГО пользователя НЕ должен загрузить его сессию — ни из ОЗУ, ни
        # с диска (закрытие cross-user IDOR). Иначе — заводим новую.
        if session_id and session_id in self.sessions:
            cached = self.sessions[session_id]
            if cached.owner_id == auth_user_id:
                return cached
            # session_id принадлежит другому пользователю — игнорируем кэш.
        if session_id:  # нет в ОЗУ (для этого владельца) — пробуем диск
            loaded = _load_session(session_id, auth_user_id)  # путь в namespace владельца
            if loaded is not None and loaded.owner_id == auth_user_id:
                self.sessions[loaded.session_id] = loaded
                return loaded
        session = Session()
        session.owner_id = auth_user_id
        self.sessions[session.session_id] = session
        return session

    async def query_stream(
        self,
        message: str,
        session_id: str = "",
        doc_ids: list[str] | None = None,
        owner_id: int | None = None,
        project_id: str = "",
        auth_user_id: int | None = None,
        role: str = "",
        pinned_tool: str = "",
        pinned_tool_args: dict | None = None,
        gateway_session_key: str = "",
        active_doc_id: str = "",
    ) -> AsyncGenerator[dict, None]:
        # Владелец сессии = реальная личность (auth_user_id); для админа
        # owner_id=None (фильтр по владельцу документа), но сессия всё равно привязана к его
        # настоящему id. Fallback на owner_id для старых не-stream вызовов.
        sess_owner = auth_user_id if auth_user_id is not None else owner_id
        session = self.get_or_create_session(session_id, auth_user_id=sess_owner)
        if doc_ids:
            session.doc_ids = doc_ids

        # Identity for per-user tools (watchlist/sources) + traces. Falls
        # back to owner_id for backward compat with old callers.
        tool_user_id = auth_user_id if auth_user_id is not None else owner_id

        from finengine import observability as _obs  # noqa: PLC0415
        _obs.emit(
            user_id=tool_user_id, session_id=session.session_id,
            event="session_start",
            project_id=project_id or None,
            message=message,
            doc_ids=session.doc_ids,
        )

        yield {"type": "session_id", "session_id": session.session_id}

        # Kill-switch: ops can disable the agent loop without restarting
        # by touching data/KILL_SWITCH or setting FINENGINE_AGENT_KILL_SWITCH.
        from finengine.policy.killswitch import is_killed
        killed, reason = is_killed()
        if killed:
            logger.warning("agent loop refused — kill-switch on (%s)", reason)
            yield {
                "type": "error",
                "error": f"Агент временно отключён администратором: {reason}",
            }
            return

        # 0. If the user attached PDFs that aren't parsed yet (.txt sidecar
        # not on disk), emit `thinking` events while we wait for the
        # BackgroundTask to finish — instead of asking the user to retry.
        # `thinking` is a transient single-line indicator on the frontend
        # (replace, not append); when the real answer starts streaming it
        # disappears, so the final message stays clean.
        # Most reports parse in 5-30s; hard timeout 5 min as a safety net.
        if session.doc_ids:
            import asyncio
            import time as _t

            from finengine.store.blobs import document_text_exists
            # Готово = текст ЕСТЬ И НЕПУСТ, тот же критерий, что у статуса
            # документа и у тула. Прежде здесь спрашивалось только
            # существование файла — полу-записанный сайдкар считался готовым.
            pending = [d for d in session.doc_ids if not document_text_exists(d)]
            if pending:
                phases = [
                    "📄 Загружаю документ…",
                    "🔎 Читаю PDF и распознаю таблицы…",
                    "📑 Индексирую страницы…",
                    "⚙️ Готовлю контекст…",
                ]
                yield {"type": "thinking", "text": phases[0]}
                deadline = _t.time() + 300
                next_phase_at = _t.time() + 6
                phase_idx = 1
                while pending:
                    if _t.time() > deadline:
                        yield {
                            "type": "text_delta",
                            "text": (
                                "⚠️ PDF не удалось обработать за 5 минут — "
                                "попробуйте загрузить документ ещё раз."
                            ),
                        }
                        yield {"type": "done"}
                        return
                    await asyncio.sleep(1.5)
                    pending = [d for d in pending if not document_text_exists(d)]
                    if pending and _t.time() >= next_phase_at and phase_idx < len(phases):
                        yield {"type": "thinking", "text": phases[phase_idx]}
                        phase_idx += 1
                        next_phase_at = _t.time() + 6

        # 1. Build full-context: whole text of each selected document.
        context, sources = _build_context(message, session.doc_ids or None, owner_id=owner_id)
        _gate_fired = False  # Срез 29: гейт ниже включает это, если сработал

        # Token guard: rough estimate (4 chars ≈ 1 token, ×1.95 for Cyrillic
        # in non-English tokenizers). Hard ceiling 800K real tokens to leave
        # headroom inside Qwen's 1M window.
        approx_real_tokens = int(len(context) / 4 * 1.95)
        if approx_real_tokens > 800_000:
            err = (
                f"⚠️ Слишком много документов выбрано (~{approx_real_tokens // 1000}K токенов). "
                f"Снимите часть отчётов из контекста или задайте вопрос по одному документу."
            )
            yield {"type": "text_delta", "text": err}
            yield {"type": "done"}
            return

        # 1.5. INGRESS v2 (Срез 31, docs/build-plan/31-vozvrat-k-zamyslu-vladeltsa-
        # ingress-v2.md). КАЖДОЕ сообщение пользователя сначала попадает в модельный
        # атом разбора (`intent.parse_message`), и модель решает: передать запрос
        # планировщику (`route="plan"` — запрос подходит под один из GoalType) или
        # ответить самой по контексту (`route="answer"` — ход идёт свободному
        # диспетчеру с его инструментами). Регэксп-переопределений решения модели
        # нет. Гейт стоит ЗДЕСЬ — после контекста, до `prompt_parts` — и
        # применяется НЕЗАВИСИМО от `gateway_session_key` (инлайн-режим реально
        # исполняется в проде: `/query/stream` никогда не передаёт
        # gateway_session_key; проверено по коду).
        #
        # Канонический конвейер исполняется, только если у хода один адресуемый
        # документ (единственный прикреплённый либо активный). Несколько
        # документов без активного — свободный путь, как и раньше;
        # многодокументная маршрутизация — задокументированный пробел (`DocumentSet`).
        _gate_doc_id = ""
        if len(session.doc_ids or []) == 1:
            _gate_doc_id = session.doc_ids[0]
        elif active_doc_id and active_doc_id in (session.doc_ids or []):
            _gate_doc_id = active_doc_id

        _ingress_decision = None
        yield {"type": "thinking", "text": "Разбираю запрос…"}
        try:
            from finengine.skills.atoms.intent import parse_message
            _ingress_decision = await parse_message(
                message,
                context=_build_ingress_context(session, active_doc_id=active_doc_id, ui_role=role),
            )
        except Exception:  # noqa: BLE001 — сбой разбора не должен ронять ход необработанным исключением
            logger.error(
                "intent.parse failed session=%s docs=%s query=%r",
                session.session_id, session.doc_ids, message[:200], exc_info=True,
            )
            if session.doc_ids:
                # Документ прикреплён, а маршрут неизвестен: свободный диспетчер
                # получил бы сырой текст и мог бы выписать число мимо конвейера.
                # Ход заканчивается типизированным отказом; без документа
                # опасности нет — ход идёт диспетчеру.
                from finengine.skills.result_events import typed_error_event

                _parse_refusal = typed_error_event(
                    code="intent_parse_failed",
                    message="не удалось разобрать запрос; повторите его, пожалуйста",
                    retryable=True,
                )
                yield _parse_refusal
                _parse_message = _parse_refusal["error"]["message"]
                session.add("user", message)
                session.add("assistant", f"[Типизированный отказ] {_parse_message}")
                _save_session(session)
                _obs.emit(
                    user_id=tool_user_id, session_id=session.session_id,
                    event="session_done", response=_parse_message,
                    response_chars=len(_parse_message), usage=None,
                )
                yield {"type": "done"}
                return

        if _ingress_decision is not None and _ingress_decision.route == "plan" and not _gate_doc_id:
            # Решение model-router `plan` обязательно: его нельзя молча заменить
            # свободным диспетчером только потому, что scope документа пока не
            # выражается одним id. До полноценного DocumentSet это честный
            # типизированный отказ, не числовой обход конвейера.
            from finengine.skills.result_events import typed_error_event

            _scope_code = "document_required" if not session.doc_ids else "document_scope_ambiguous"
            _scope_message = (
                "для финансового конвейера приложите отчёт"
                if not session.doc_ids
                else "выберите один активный отчёт; многодокументный план пока не собран"
            )
            _scope_refusal = typed_error_event(
                code=_scope_code, message=_scope_message, retryable=True,
            )
            yield _scope_refusal
            session.add("user", message)
            session.add("assistant", f"[Типизированный отказ] {_scope_message}")
            _save_session(session)
            _obs.emit(
                user_id=tool_user_id, session_id=session.session_id,
                event="session_done", response=_scope_message,
                response_chars=len(_scope_message), usage=None,
            )
            yield {"type": "done"}
            return

        if _ingress_decision is not None and _ingress_decision.route == "plan" and _gate_doc_id:
            _gate_case_id = project_id or session.session_id
            # События конвейера (`plan_graph`, `node_run`, ...) уходят клиенту СРАЗУ,
            # по мере появления (`stream_blocking`), а не копятся до конца работы.
            # `tool_call` — перед первым событием: чип «работаю» появляется вместе с
            # планом, а не после результата.
            _gate_tail_events: list[dict] = []  # события, рождённые самим агентом (отказ при сбое гейта)
            _gate_diag: dict = {}  # контекст для ERROR-лога
            _gate_outcome = None
            _gate_args = {"query": message}
            _gate_call_sent = False
            # ⚠️ ВЕСЬ БЛОК — ОДНО try/except, а не только вызов конвейера. Резолв
            # метаданных/текста документа — тоже часть гейта: сбой ЗДЕСЬ (например,
            # хранилище документов не настроено на этом стенде) не должен ронять ход
            # необработанным исключением.
            try:
                _gate_meta = get_store().get_document(_gate_doc_id)
                _gate_owned = _gate_meta is not None and (
                    owner_id is None or not _gate_meta.owner_id or _gate_meta.owner_id == owner_id
                )
                _gate_text = ""
                if _gate_owned:
                    from finengine.store.blobs import document_text as _raw_document_text
                    _gate_text = _raw_document_text(_gate_doc_id) or ""
                if _gate_text:
                    from finengine.orchestrator.live_stream import stream_blocking
                    from finengine.skills.production_runtime import execute_routed_goal
                    async for _gate_kind, _gate_payload in stream_blocking(
                        execute_routed_goal,
                        owner_id=tool_user_id, case_id=_gate_case_id,
                        document_id=_gate_doc_id, document_text=_gate_text,
                        goal=_ingress_decision.goal, diagnostics=_gate_diag, cancel_kw="is_cancelled",
                    ):
                        if _gate_kind == "event":
                            if not _gate_call_sent:
                                _gate_call_sent = True
                                session.add_tool_call("financial_pipeline", _gate_args)
                                yield {
                                    "type": "tool_call", "name": "financial_pipeline",
                                    "args_summary": _summarize_tool_args("financial_pipeline", _gate_args),
                                }
                            yield _gate_payload
                        else:
                            _gate_outcome = _gate_payload
            except Exception:  # noqa: BLE001 — mandatory evidence boundary is fail-closed
                logger.error(
                    "evidence gate failed session=%s case=%s doc=%s intent_kind=%s query=%r",
                    session.session_id, _gate_case_id, _gate_doc_id,
                    _gate_diag.get("intent_kind", "-"), message[:200], exc_info=True,
                )
                from finengine.skills.production_runtime import EvidenceGateOutcome
                from finengine.skills.result_events import typed_error_event

                _gate_refusal = typed_error_event(
                    code="evidence_gate_failed",
                    message=(
                        "канонический финансовый конвейер временно недоступен; "
                        "числовой ответ из текста документа не сформирован"
                    ),
                    retryable=True,
                )
                _gate_tail_events.append(_gate_refusal)
                _gate_outcome = EvidenceGateOutcome(
                    tool_name="financial_pipeline",
                    manifest={
                        "schema_version": "1.0", "case_id": _gate_case_id,
                        "document_id": _gate_doc_id, **_gate_refusal,
                    },
                )
            if _gate_outcome is not None:
                import json as _gate_json

                _gate_fired = True
                if not _gate_call_sent:
                    # Гейт сработал, но событий не породил (например, отказ до плана).
                    session.add_tool_call(_gate_outcome.tool_name, _gate_args)
                    yield {
                        "type": "tool_call", "name": _gate_outcome.tool_name,
                        "args_summary": _summarize_tool_args(_gate_outcome.tool_name, _gate_args),
                    }
                for _gate_ev in _gate_tail_events:
                    yield _gate_ev
                _gate_ok = "error" not in _gate_outcome.manifest
                session.add_tool_output(
                    _gate_outcome.tool_name, _gate_ok,
                    _gate_json.dumps(_gate_outcome.manifest, ensure_ascii=False),
                )
                yield {"type": "tool_output", "name": _gate_outcome.tool_name, "ok": _gate_ok}
                if not _gate_ok:
                    # Запрос модель уже передала планировщику, а канонический
                    # результат — типизированный отказ. Ход заканчивается ЗДЕСЬ: UI
                    # уже получил typed_error, диспетчер не получает ни prompt, ни
                    # текст документа (числовой обход закрыт — промпт-запрет здесь
                    # принципиально недостаточен).
                    _gate_message = str(
                        (_gate_outcome.manifest.get("error") or {}).get("message")
                        or "Канонический финансовый конвейер не смог выполнить запрос."
                    )
                    session.add("user", message)
                    session.add("assistant", f"[Типизированный отказ] {_gate_message}")
                    _save_session(session)
                    _obs.emit(
                        user_id=tool_user_id, session_id=session.session_id,
                        event="session_done", response=_gate_message,
                        response_chars=len(_gate_message), usage=None,
                    )
                    if sources:
                        yield {"type": "sources", "sources": sources}
                    yield {"type": "done"}
                    return
                # The canonical cards are already the answer. A second model
                # call is not merely redundant: the configured gateway owns
                # server-side tools even when no client-tool definitions are
                # sent, so it can reopen extraction outside this evidence gate.
                _gate_result = _gate_outcome.manifest.get("result") or {}
                _gate_status = str(_gate_result.get("status") or "supported")
                _gate_message = (
                    "Канонический финансовый конвейер завершён частично; "
                    "доступные результаты и их статусы показаны в карточках выше."
                    if _gate_status in {"partial", "missing", "conflicting", "failed"}
                    else "Готово: проверенные результаты опубликованы в карточках выше."
                )
                # Итог цикла гибридного планировщика: его доказательства — не всегда карточки
                # UI (цитата, `dyn:*`), поэтому пользователь видит ответ планировщика.
                _gate_message = str(
                    (_gate_outcome.manifest.get("dynamic_run") or {}).get("answer") or _gate_message
                )
                # Детерминированный путь не выполнил часть просьбы — сказать об этом явно.
                _gate_unperformed = _gate_outcome.manifest.get("not_performed") or []
                if _gate_unperformed:
                    _gate_message += "\n\nНе выполнено: " + "; ".join(_gate_unperformed) + "."
                session.add("user", message)
                session.add("assistant", _gate_message)
                _save_session(session)
                _obs.emit(
                    user_id=tool_user_id, session_id=session.session_id,
                    event="session_done", response=_gate_message,
                    response_chars=len(_gate_message), usage=None,
                )
                yield {"type": "text_delta", "text": _gate_message}
                if sources:
                    yield {"type": "sources", "sources": sources}
                yield {"type": "done"}
                return

        # Rolling compaction (C): уплотняем длинную историю перед сборкой
        # промпта (триггер высокий — короткие диалоги не задевает).
        if len(session.history) > _SUMMARY_TRIGGER:
            yield {"type": "thinking", "text": "уплотняю историю диалога…"}
            await _maybe_summarize(session)

        # 2. Build full prompt
        history_text = session.format_history()
        prompt_parts = []

        # Today's date — injected dynamically so the model can't fall
        # back on its training cutoff for "what is today". Critical for
        # the IR Daily Digest playbook ("новости за СЕГОДНЯ"), where
        # without this the agent silently dredged news from 2025.
        from datetime import datetime as _dt
        today = _dt.now()
        prompt_parts.append(
            f"## Сегодняшняя дата: {today.strftime('%Y-%m-%d (%A)')}.\n"
            "Все новости и публикации, которые ты собираешь, должны быть **за последние 24-48 часов** "
            "относительно этой даты. Если результат поиска не имеет явной даты или датирован раньше — "
            "**не включай его в ответ**. Не используй знания из training-set'а как «свежие новости»."
        )

        # #88: настройки автоматизаций из чата (окно календаря, расписание,
        # фокус дайджеста) — правит fe_scenario_settings, НЕ пересказ инструкции.
        prompt_parts.append(
            "## Настройки автоматизаций (сценариев)\n\n"
            "Когда пользователь просит ИЗМЕНИТЬ настройки автоматизаций — период/"
            "окно календаря («смотри три недели назад», «месяц вперёд»), ФОКУС дайджеста («собирай только по Европе» → instructions), расписание "
            "(«запускай по будням в 10»), паузу — или спрашивает текущие настройки: "
            "вызови `fe_scenario_settings` (сначала action=list, возьми scenario_id "
            "нужного сценария по kind/имени; затем action=update). Окно календаря — "
            "`params.back_days`/`params.fwd_days` В ДНЯХ (три недели = 21). "
            "Расписание меняй ТОЛЬКО по явной просьбе. После update коротко "
            "подтверди: что поменял и что применится при следующем запуске "
            "автоматизации (запустить можно в разделе «Автоматизации»).\n\n"
            "Если просьба МЕНЯЕТ ПОВЕДЕНИЕ/КОД навыка сверх ручек (новая "
            "колонка/метрика/секция, другой формат) — `fe_skill_editor` "
            "(list → propose). Честно скажи: правка сохранена патчем и будет "
            "активирована с ближайшим обновлением (не мгновенно)."
        )

        # Project sources awareness — ВСЕГДА (на каждом ходу) инжектим
        # список xlsx/pdf/csv-файлов в Sources проекта, чтобы модель на
        # follow-up'ах не отвечала "загрузите файлы". Без этого после
        # первого факторного анализа на вопрос "как считал?" модель
        # отвечала что данных нет — потому что триггер по ключевым словам
        # не срабатывал, и о существовании sources она не знала.
        try:
            from finengine.skills import sources as _sources
            # ВАЖНО: использовать auth_user_id, не owner_id. Для админов
            # owner_id = None (фильтр по владельцу документа), а sources хранятся под
            # настоящим user.id — поэтому смотрим только в auth_user_id.
            # И project_id (UUID чата/проекта), не session_id (разговорная
            # сессия — это другой UUID).
            _src_list = (
                _sources.list_sources(user_id=auth_user_id, project_id=project_id)
                if auth_user_id and project_id else []
            )
        except Exception:
            _src_list = []
        if _src_list:
            src_lines = []
            for m in _src_list[:20]:  # cap to avoid prompt bloat
                src_lines.append(
                    f"  - {m.filename} (file_id={m.file_id[:12]}…, "
                    f"{m.content_type or 'unknown'})"
                )
            prompt_parts.append(
                "## 🟢 Источники проекта — ФАЙЛЫ УЖЕ ЗДЕСЬ\n\n"
                f"В этом проекте {len(_src_list)} постоянный(ых) файл(ов), "
                "загруженных пользователем:\n"
                + "\n".join(src_lines) + "\n\n"
                "**🚫 ЗАПРЕЩЕНО отвечать:** «загрузите файлы», «у меня нет данных», "
                "«предоставьте документы», «дайте идентификаторы», «укажите file_id», "
                "«пожалуйста, дайте команду в формате factor_analysis(plan_file_id=…)». "
                "Файлы УЖЕ есть. Доступны на ВСЁМ протяжении этого чата — не нужно их "
                "«помнить», backend tools достанут их при вызове. **Все backend-tools "
                "auto-detect'ят файлы по именам** — вызывай ИХ без file_id-аргументов.\n\n"
                "Вопрос пользователя про эти данные — повод ОТКРЫТЬ их инструментом, а не "
                "ответить по памяти о прошлом ходе. Каким именно инструментом — выбирай по "
                "их описаниям и по типу файла."
            )

        # Clarification policy — ask the user (option cards via ask_user) only
        # when genuinely uncertain; otherwise proceed with sensible defaults.
        prompt_parts.append(
            "## Уточняющие вопросы — ТОЛЬКО через инструмент ask_user\n\n"
            "Инструмент `ask_user(question, options)` показывает пользователю вопрос с 2–5 "
            "вариантами и возвращает управление ему (его выбор придёт следующим сообщением).\n\n"
            "**ГЛАВНОЕ ПРАВИЛО:** если тебе нужно что-то уточнить у пользователя, чтобы не "
            "домысливать — задавай это **ТОЛЬКО вызовом `ask_user` с вариантами**, и НИКОГДА не "
            "пиши уточняющий вопрос простым текстом («уточните…», «какую компанию?»). Текстовый "
            "встречный вопрос — запрещён; вместо него вызывай `ask_user`.\n\n"
            "Когда уточнять (РЕДКО): задача допускает несколько РАВНОВЕРОЯТНЫХ трактовок, "
            "для которых НЕТ разумного дефолта, и неверный выбор сделает работу бесполезной; "
            "либо перед дорогой операцией (полный `ir_calendar_run` — минуты) при неясном "
            "объёме; либо перед необратимым действием. Само по себе отсутствие явного периода / "
            "метрики / вида анализа — НЕ повод спрашивать: бери дефолт (все доступные периоды отчёта, "
            "полное покрытие, analysis_type='general'). Если документ уже приложен — компания "
            "уже задана этим документом, НЕ спрашивай «какую компанию/какой файл».\n"
            "Пример: на «Сделай обзор по компании» без названия → "
            "`ask_user(\"По какой компании сделать обзор?\", [\"Сбербанк\", \"Газпром\", \"Лукойл\", \"Другая (впишу)\"])`.\n\n"
            "**НО НЕ переспрашивай по мелочам:** почти всегда есть разумный дефолт — действуй сам. "
            "Дефолты для анализа/извлечения: период = ВСЕ доступные периоды отчёта, покрытие = полное, "
            "вид анализа = general. Если документ приложен ИЛИ запрос выполним с этими дефолтами — "
            "СРАЗУ выполняй, максимум оговори допущение одной строкой; ask_user в этом случае НЕ вызывай. "
            "Один вопрос за раз. Если пользователь уже всё указал — сразу выполняй."
        )

        # Блоки ниже подвешены на ФАКТЫ контекста (xlsx в Источниках, приложен
        # отчёт), а не на слова запроса. Чем работать — решает модель по
        # описаниям инструментов и навыков.
        #
        # #237, 14.08.2026: отсюда снят блок «Личная база знаний чата», который
        # включался регулярной `https?://\S+` по тексту пользователя. Это был
        # разбор запроса кодом — ровно то, что в комментарии над ним значилось
        # запрещённым («NOT on keyword-matching the user's message»), и ровно
        # то, что владелец назвал условным программированием вместо агента.
        # Разница между `kb_add_url` и `fetch_url` (первый сохраняет в базу
        # чата, второй нет) переехала в ОПИСАНИЕ `kb_add_url`: это свойство
        # инструмента, а не свойство хода, и знать о нём надо всегда, а не
        # только когда в сообщении оказалась ссылка.
        _has_xlsx = any(
            (m.filename or "").lower().endswith((".xlsx", ".xls")) for m in _src_list
        )

        # xlsx plan/fact in Sources → factor-analysis guidance (factual gate).
        # The agent calls factor_analysis only if the user actually wants it.
        if _has_xlsx:
            prompt_parts.append(
                "## Factor Analysis — используй backend tool, не считай вручную\n\n"
                "Пользователь просит провести факторный анализ отклонения Факт-План. "
                "У тебя есть готовый backend-tool `factor_analysis(plan_file_id, "
                "fact_file_id, nomenclature_file_id, preset, products, factors, "
                "period_label)`. Он сам:\n"
                "  - читает xlsx план/факт (openpyxl, не IDP-OCR)\n"
                "  - считает 9 факторов по ТЗ (цепная подстановка для выручки, "
                "Laspeyres для статей себестоимости)\n"
                "  - возвращает агрегаты, факторы и per-product breakdown с "
                "**точно сошедшимися** цифрами (Σ факторов = Δполн без остатка).\n\n"
                "**Что делать:**\n"
                "1. Вызови `list_sources` → найди xlsx с планом/фактом (auto-detect "
                "по именам тоже работает: 'план/plan' vs 'факт/fact').\n"
                "2. Вызови `factor_analysis(period_label='Q1 2026')` — без `preset`, "
                "по умолчанию работает полный набор 9 факторов + маржинальная "
                "декомпозиция (3 фактора). Передавай `preset='mvp'` ТОЛЬКО если "
                "пользователь явно попросил «коротко».\n"
                "3. В ДЕФОЛТНОМ ответе пользователю (без явного запроса на детали):\n"
                "   - 1 строка заголовка: «Q1 2026: выручка +1.6%, маржа −X% (−Y млн ₽)»\n"
                "   - **3 маржинальных фактора** в МЛН ₽ (округляй до целых млн "
                "или одной десятой): объём/структура, цена, себестоимость/ед.\n"
                "     ▸ цитируй из `factors[].value` где `group=='margin'`, делай /1_000_000\n"
                "   - 1-2 предложения интерпретации с указанием продукта-драйвера "
                "(из `products_breakdown[].margin_delta` — кто самый отрицательный)\n"
                "   - **Финал строкой**: «📎 Детализация в xlsx-файле <ИМЯ_ФАЙЛА>» — "
                "вместо <ИМЯ_ФАЙЛА> ПОДСТАВЬ ФАКТИЧЕСКОЕ ЗНАЧЕНИЕ из tool-output: "
                "поле `xlsx_artifact.filename`, например `factor_analysis_Q1_2026.xlsx`. "
                "❌ НЕ пиши `xlsx_artifact.filename` как литерал — это шаблон, не "
                "конечный текст. Файл уже в Источниках проекта.\n"
                "   - **НЕ показывай формулы, method_notes, по-продуктовые таблицы, "
                "Δнорма+Δобъём, числа в миллиардах, by_product** в дефолтном "
                "ответе. Это всё в xlsx и по follow-up'у.\n\n"
                "**Follow-up в той же ветке диалога:**\n"
                "  - 📜 **ВАЖНО:** История диалога ниже содержит блоки `[Tool result: "
                "factor_analysis (OK)]` с ПОЛНЫМ JSON-выводом предыдущих вызовов. "
                "Aggregates / factors / method_notes / xlsx_artifact доступны "
                "целиком. by_product у каждого фактора тоже там.\n"
                "  - 🚫 **НЕ вызывай factor_analysis повторно**, если в истории "
                "уже есть его tool result. Это лишняя трата токенов и времени.\n"
                "  - 🚫 **НЕ запрашивай у юзера file_id** — handler сам auto-detect'ит "
                "файлы. Но в follow-up'е tool ВООБЩЕ не должен вызываться, кроме "
                "случая «пересчитай с другими параметрами».\n"
                # #237: здесь стояли фразовые списки вопросов («как считал», «по
                # какой формуле», «детализируй фактор X») с готовым ответом на
                # каждый. Что спросили — понимает модель; промпт называет только
                # ГДЕ лежит ответ.
                "  - Спрашивают о методе, формуле или разбивке — ответ уже в этом "
                "блоке истории: `factors[*].formula`, `method_notes`, "
                "`factors[*].by_product`. Процитируй их СЛОВАМИ, с цифрами в млн ₽, "
                "не вызывая тул заново.\n"
                "  - Просят пересчитать с другими параметрами (иной пресет, отбор "
                "продуктов) — вот теперь повторный вызов уместен.\n"
                "  - 🚫 Никогда не отвечай «загрузите файлы», «дайте идентификаторы».\n\n"
                "**Формат чисел в ответе:**\n"
                "  - Все суммы в **млн ₽**, без научной нотации (НЕ `1e6`, НЕ `e+06`).\n"
                "  - Округление до 0.1 млн (`−289.3 млн ₽`) или до целых (`−331 млн ₽`).\n"
                "  - Минус-знак — настоящий «−» или ASCII `-`, без скобок-«счетовод».\n"
                "  - Российский формат с пробелами в тысячах если показываешь рубли "
                "(`331 824 400 ₽`), но лучше млн.\n\n"
                "**ЗАПРЕЩЕНО:**\n"
                "- 🚫 **Python-блоки с расчётами в ответе** — НЕ пиши ```python ... ```, "
                "НЕ показывай `delta_price = -77.4e6`, НЕ показывай `* 1e-6`, НЕ показывай "
                "`print(f\"...\")`. Это НЕ thinking, это шум. Расчёты уже сделаны в backend, "
                "в ответе только готовые цифры человеческим текстом.\n"
                "- 🚫 Считать факторы в голове / читать xlsx через `read_source` / "
                "пересчитывать значения — всё уже в tool-output, бери as-is.\n"
                "- 🚫 Использовать `fetch_url` / `tavily_search` — данные локально, в sources.\n"
                "- 🚫 Выдавать сырой JSON tool-результата — переписывай в человеческий "
                "markdown с таблицей top-3 и абзацем выводов.\n"
                "- 🚫 Громоздкие формулы в дефолтном ответе. Формулы (`Σ Q₁ × (P₁−P₀)`) — "
                "ТОЛЬКО если юзер явно спросил «по какой формуле».\n"
                "- 🚫 Не вставляй `code block` для словесных комментариев. `code block` "
                "только для **реальных команд** или **констант**."
            )
        # Отчёт прикреплён. Чем выполнить просьбу, модель выбирает по описаниям
        # навыков — здесь маршрута ей никто не даёт (#237/#166).
        # #160: сторож от дубля живёт РОВНО ОДИН ход. Стираем статусы прошлого
        # хода здесь, в самом его начале, — до того как что-либо запустится.
        # Иначе «извлеки заново» следующим сообщением молча вернуло бы прошлый
        # результат: дубль стоит времени, а подмена чисел стоит доверия ко всем
        # числам сразу.
        #
        # Чистка ДВУМЯ приёмами, и вне ветки «есть документы в сессии»: по
        # составу сессии (там же снимается и «идёт», чтобы пересчёт по просьбе
        # был возможен) и по владельцу для всех ЗАВЕРШЁННЫХ. Второе — потому что
        # состав сессии меняется: документ выпадает из набора проекта, запись
        # «готово» остаётся, и явный «извлеки вот этот» получает молчаливый
        # отказ. Раньше блок стоял ВНУТРИ `if session.doc_ids:` и на таком ходу
        # не выполнялся вовсе.
        from finengine.skills import grounding_bus as _bus_turn  # noqa: PLC0415
        for _d in (session.doc_ids or []):
            _bus_turn.run_forget(session.owner_id, _d)
        _bus_turn.run_forget_finished(session.owner_id)

        # Нонс ХОДА выводится из ключа сессии гейтвея — той же функцией, что у
        # раннера (`api/station_turn.run_nonce_of`). Инлайн-ветка ключа не имеет,
        # там нонсы выдаёт пул воркеров ниже; здесь пусто, и блок не эмитится.
        from finengine.api.station_turn import (  # noqa: PLC0415
            analysis_nonce_of, run_nonce_of)
        _turn_nonce = run_nonce_of(gateway_session_key or "")
        _analysis_nonce = analysis_nonce_of(_turn_nonce)

        if session.doc_ids:
            prompt_parts.append(
                "## Финансовый отчёт приложен\n\n"
                "Документ УЖЕ здесь (`doc_id` в заголовке его блока «## Документ: … [doc_id: …]»). "
                "НИКОГДА не отвечай «нет документа / загрузите файл» и НИКОГДА не вызывай `ask_user`, "
                "когда отчёт приложен и запрос выполним с дефолтами — ни про файл, ни про период, "
                "ни про метрики, ни про вид анализа. Дефолты: период = ВСЕ доступные периоды отчёта, "
                "покрытие = полное, вид анализа — базовый. Сразу выполняй на приложенном документе "
                "и при желании оговори допущение одной строкой.\n\n"
                # НОНС ХОДА — ФАКТ ЭТОГО ХОДА, И ОН ОБЯЗАН ДОЕХАТЬ ДО МОДЕЛИ.
                #
                # Дыра, открывшаяся вместе со снятием карты (#237 → #244). Тело
                # навыка извлечения говорит: «идентификатор документа и нонс
                # прогона выдаёт система в этом ходе — используй РОВНО их». В
                # ИНЛАЙН-ветке система их и выдавала (пул нонсов для воркеров).
                # В ГЕЙТВЕЙ-петле пул пуст по решению Ш11 — и нонс модели не
                # называл никто, хотя канал хода на него уже подписан
                # (`main.py` → `station_turn.arm(nonce=run_nonce_of(gw_key))`).
                #
                # Пока маршрут был прибит картой, это не всплывало: модель шла в
                # тул, а тул заводит свой нонс сам. Сняли карту — модель вправе
                # пойти по телу навыка, и там команда без нонса означает записку
                # доставки с пустым полем: бэкенд отвечает 403 по скоупу, скрипт
                # честно печатает FETCH-DENIED и выходит с кодом 3. Прогон
                # падает на первом шаге, и выглядит это как поломка станции.
                #
                # Нонс НЕ передаётся отдельным каналом и не хранится: он
                # ВЫВОДИТСЯ из ключа сессии одной функцией — той же, которой
                # пользуется раннер хода. Два источника разъехались бы молча.
                + (f"НОНСЫ ЭТОГО ХОДА (для команд станции). Используй РОВНО их, не "
                   f"выдумывай и не переставляй местами с `doc_id`:\n"
                   f"- извлечение и расчёт: `{_turn_nonce}`\n"
                   f"- разбор (анализ): `{_analysis_nonce}`\n"
                   f"У каждого вида работы СВОЙ нонс — он адресует её результат. "
                   f"Пустишь разбор на нонсе извлечения — доставлен будет только "
                   f"один из двух, и молча.\n\n" if _turn_nonce else "")
                # #237: здесь стояла вторая карта «намерение → инструмент» —
                # «анализ/оцени/что скажешь → fe_analyze», «извлеки/таблица →
                # fe_extract_metrics», — с оговоркой «НЕ по точным словам»
                # прямо над списком точных слов. Чем выполнить просьбу, модель
                # выбирает по описаниям инструментов и навыков; наш промпт
                # рассказывает только то, чего в описаниях быть не может, —
                # обстоятельства ЭТОГО хода.
                + "OCR запускается АВТОМАТИЧЕСКИ при загрузке отчёта — работающие с отчётом "
                "инструменты САМИ дождутся готового текста, отдельно его ждать не нужно. "
                "`fe_idp_ocr(doc_id)` дёргай ТОЛЬКО как фоллбэк — если тул вернул, что текст "
                "так и не распознался; НЕ запускай его поверх уже идущего OCR. Инструменты "
                "сами классифицируют отчёт; `fe_classify_report` — опционально, показать ячейку. "
                "`role` передавай ТОЛЬКО если юзер назвал роль (other/risks/baifp/die/cib/dgr).\n\n"
                "**Таблицу прогона и готовый отчёт разбора система показывает сама** — "
                "и таблицу, и карточку пользователь уже видит. Не пересказывай их и не "
                "выводи повторно: дай 2-3 предложения итога (вердикт + главный риск) и, "
                "если параллельно шла другая работа, строку о её статусе. НЕ считай и не "
                "дополняй показатели вручную.\n\n"
                "### Считать в уме ЗАПРЕЩЕНО\n"
                "Любой показатель или коэффициент, которого нет в готовой таблице, считай "
                "КОДОМ — навыком расчёта: он берёт уже извлечённые числа и возвращает "
                "результат вместе с формулой и подставленными входами, строкой в таблице. "
                "Это не формальность: число, посчитанное в уме, пользователь не может "
                "проверить, а расхождение с таблицей выглядит как ошибка аналитика.\n"
                "Не хватает входа — расчёт назовёт недостающее ПОИМЁННО и скажет, искали его "
                "в отчёте или не заказывали. Поправь формулу или скажи прямо, что данных "
                "нет. Прикидывать «примерно» нельзя.\n\n"
                "### Не умеешь — скажи, что не умеешь\n"
                "Если просьба требует того, чего в системе НЕТ, скажи это прямо и назови, что "
                "именно недоступно. Худший ответ — сделать вид, что справился: посчитать в уме, "
                "выдать оценку за извлечённое значение или пересказать правило вместо того, чтобы "
                "его применить. Пользователь строит на твоих числах решения и не может отличить "
                "добытое от придуманного.\n"
                "Частые случаи: значения нет в таблице извлечения и нет входа для расчёта — "
                "значит данных нет; просят достать конкретную строку из примечания — сегодня "
                "такого инструмента нет; просят формат выгрузки, которого нет. Во всех случаях "
                "честный отказ с объяснением полезнее правдоподобного ответа.\n\n"
                "### Пользователь поправил ПРАВИЛО — запиши это в методику\n"
                "Отличай поправку к ЧИСЛУ («пересчитай без разовой продажи» → расчёт) от "
                "поправки к ПРАВИЛУ: «работающие активы считай так-то», «NIM у нас по средним "
                "за квартал», «горизонт устойчивости 8 месяцев, а не 6», «не пиши раздел про "
                "качественные факторы». Правило — это `fe_methodology`. Начни с "
                "`action=\"list\"`, найди элемент, потом `set`/`add`/`disable`.\n"
                "ОБЯЗАТЕЛЬНО различай горизонт. «В этот раз / сейчас» → `scope=\"turn\"`: "
                "учтётся ближайшим анализом и забудется. «Всегда / у нас так принято / в нашем "
                "банке» → `scope=\"always\"`: изменит ВСЕ будущие отчёты этого пользователя. Из "
                "фразы не ясно — спроси (`ask_user`) ПЕРЕД записью always.\n"
                "Записал — скажи об этом одной строкой: что именно изменил и что это отменяемо. "
                "Дописывать правило пересказом в свой ответ ЗАПРЕЩЕНО: правка, не попавшая в "
                "методику, исчезнет к следующему отчёту, а пользователь будет уверен, что "
                "считают по его правилам.\n\n"
                "### Говори, что делаешь — ПЕРЕД каждым вызовом инструмента\n"
                "Перед тем как взять инструмент, напиши 1-2 предложения: что ты понял из уже "
                "полученного и зачем берёшь именно этот инструмент. Пользователь видит эти "
                "реплики лентой хода — для него это единственный способ понять, что происходит "
                "внутри многоминутной работы.\n"
                "Пиши ФАКТАМИ из этого прогона, а не общими словами. Плохо: «Сейчас проанализирую "
                "документ». Хорошо: «Отчёт МСФО за 2024, консолидированный — беру полный реестр "
                "нефинансовой ячейки» или «В аудиторском заключении оговорка о непрерывности "
                "деятельности — проверю долговую нагрузку отдельно».\n"
                "Одна реплика на вызов, без нумерации и заголовков. Если фактов ещё нет "
                "(самый первый вызов) — коротко скажи, с чего начинаешь."
            )
            # #116 МЕХАНИЧЕСКИЙ ДИСПЕТЧЕР — граница проходит так:
            #   ЧТО пользователь хочет — решает МОДЕЛЬ (#141, решение владельца
            #     02.08.2026). Раньше здесь стоял поиск слов «извлеч|показател|
            #     метрик» И «анализ|разбор|вывод|оцен»; формулировка без них —
            #     «сделай табличку и скажи, что думаешь» — проходила мимо, и
            #     пользователь молча получал последовательную работу вместо
            #     параллельной. Ничего не ломалось, просто дольше, и причину
            #     было не увидеть. Шорткат сюда не заходит: там намерение уже
            #     сказано кнопкой, спрашивать не о чем.
            #   КАК это исполнить — решает КОД. Ждать, пока до параллели
            #     додумается агентская петля, незачем: замер 30.07 — воркер
            #     спавнился за 31 с, а его петля «собиралась» 8 минут прежде
            #     чем запустить движок. Бэкенд запускает извлечение САМ, фоновым
            #     прогоном без воркер-петли; модели остаётся только анализ.
            # Ш11: в ГЕЙТВЕЙ-режиме диспетчер молчит и о намерении не спрашиваем —
            # что делать за ход, решает петля гейтвея. Пока проверки режима не
            # было, оба пути гоняли ОДИН И ТОТ ЖЕ диспетчер: сравнение «двух
            # петель» сравнивало петли поверх одного решателя и давало ложный
            # зелёный на приёмках следующих шагов.
            #
            # ЗАЗЕМЛЕНИЕ ПРИ ЭТОМ НЕ ТЕРЯЕТСЯ — и это обязательное условие, без
            # которого отключать диспетчер было НЕЛЬЗЯ. `_bus.announce`/`publish`
            # переехали в прогон движка (Ш12), поэтому слот создаёт сам прогон,
            # кто бы его ни запустил. Пока шина
            # принадлежала диспетчеру, эта же правка означала бы, что на
            # гейтвей-ходе «извлечение + анализ» движок отрабатывает ДВАЖДЫ —
            # молчаливый откат #121 с разными числами в таблице и в карточке.
            # #84 Фаза C: фоновое извлечение воркером (sessions_spawn). Шаблон задачи
            # доказан ведомым прогоном (SWITCHOVER_PLAN.md); полка+доставка+релей — #83/#84.
            # Нонс выдаёт БЭК (не модель): на нём детерминированная корреляция —
            # mailbox (_bg_extract_mailbox) слушает релей и кладёт итог в историю.
            # Пул нонсов (#84 мульти-воркеры): по нонсу на документ (кап 4 —
            # не заваливать qwen/infproxy параллельными гига-вызовами движка).
            # Nonce-скоуп доставки (#84 секьюрити): демон отдаст комнате ТОЛЬКО
            # документы этой сессии и только под валидным нонсом. Регистрируем
            # ВСЕГДА (скоуп ≠ взвод mailbox ниже: тот только при намёке на фон).
            # Ш11: в гейтвей-режиме воркеров не взводим, и дело не только в
            # аккуратности. Саб-агент СЕГОДНЯ ФИЗИЧЕСКИ НЕ МОЖЕТ позвать ни один
            # fe_*-тул: бэкенд отдаёт 403 на незарегистрированный ключ сессии
            # (api/internal_tools.py), а ключ ребёнка не регистрируется нигде и
            # зарегистрирован быть не может. Отказ при этом НЕМОЙ — плагин
            # возвращает модели строку с ошибкой, и ход продолжается текстом.
            # То есть обещать здесь фоновых воркеров значит обещать то, чего нет
            # (открыто шагом Ш10).
            #
            # Резать надо ЗДЕСЬ, в точке РОЖДЕНИЯ пула, а не ниже на взводе
            # ящиков: нонсы уезжают модели прямо в промпте и открывают скоуп
            # выдачи текста документа. Погашенный ящик при выданном нонсе —
            # второй немой отказ поверх первого.
            bg_pool = ([] if gateway_session_key else
                       [uuid.uuid4().hex[:16] for _ in range(min(len(session.doc_ids), 4))])
            if bg_pool:
                from finengine.api.internal_docs import register_nonce_docs  # noqa: PLC0415
                for _bg_n in bg_pool:
                    register_nonce_docs(_bg_n, session.doc_ids)
                prompt_parts.append(
                    "## Фоновое извлечение — воркер через `sessions_spawn`\n\n"
                    "Если пользователь просит извлечь показатели «В ФОНЕ» / «параллельно» / «не "
                    "блокируя чат», ИЛИ просит извлечь по НЕСКОЛЬКИМ документам сразу — не веди "
                    "прогон в этом ходе (он держит ход минуты), а делегируй суб-агенту-воркеру:\n"
                    f"1. НОНСЫ запусков (выданы системой — используй РОВНО их, не выдумывай; "
                    f"один нонс = один воркер, бери по порядку): `{'`, `'.join(bg_pool)}`\n"
                    "2. Вызови `sessions_spawn` с task РОВНО по шаблону (подставь doc_id из заголовка "
                    "блока документа и СЛЕДУЮЩИЙ неиспользованный нонс из п.1; --company-type добавь "
                    "ТОЛЬКО если тип очевиден — bank или non_financial; --role только если юзер "
                    "назвал роль):\n"
                    "«У тебя есть exec в песочнице; полка .shelf/ уже распакована. Сделай по "
                    "порядку. Шаг 1 exec (долгий, жди до 15 минут): "
                    "bash .shelf/aifa_station_skills/aifa-extract/scripts/extract_run.sh "
                    "--doc-id <DOC_ID> --nonce <НОНС> [--company-type bank|non_financial] . "
                    "Если шаг 1 упал — повтори РОВНО ту же команду ещё один раз (не меняй её, "
                    "не добавляй cd, других команд не выдумывай). "
                    "Шаг 2: сообщи последние 12 строк вывода шага 1 (там итог metrics_found и компания).»\n"
                    "3. Пользователю ответь КОРОТКО: фоновое извлечение запущено, займёт ~5-10 минут, "
                    "результат можно спросить позже в этом же чате.\n"
                    "4. Несколько документов за раз — на КАЖДЫЙ документ отдельный `sessions_spawn` "
                    "со СВОИМ нонсом из списка п.1 (воркеры считают параллельно). Нонсы кончились — "
                    "остальные документы отложи и скажи об этом пользователю.\n"
                    "5. Подхват результата: итоги появляются в Истории диалога блоками "
                    "`[Tool result: background_extract]` (status, company_name, metrics_found, "
                    "main_period, excel_file_id) — по блоку на документ. Когда юзер спрашивает про "
                    "статус/результат фонового извлечения — отвечай ИЗ этих блоков своими словами, "
                    "сводкой по каждому документу. Про Excel: если в блоке ЕСТЬ `excel_file_id` — "
                    "скажи, что Excel со всеми периодами лежит в панели «Источники»; если НЕТ — про "
                    "Excel не упоминай вовсе. Если блока по документу ещё НЕТ — скажи, что его прогон "
                    "ещё идёт (~5-10 мин), НЕ переспрашивай, НЕ запускай заново без явной просьбы.\n\n"
                    "Обычное «извлеки показатели» БЕЗ слов про фон/параллельность — по-прежнему "
                    "прогоном в этом ходе, с живым прогрессом в чате."
                )
            # ПРАВИЛО ХОДА, НЕ АЛЬТЕРНАТИВА ФОНУ. До #166 этот блок стоял в
            # `else` к диспетчеру «извлечение уже запущено системой»; вместе с
            # диспетчером ветка исчезла, а правило осталось — оно про механику
            # хода и действует всегда.
            prompt_parts.append(
                # #237: здесь стояла третья редакция той же карты — «классика:
                # «проведи извлечение И краткий анализ»» с готовой парой
                # вызовов и утверждением «анализ в результатах извлечения НЕ
                # нуждается». Второе вдобавок прямо противоречит телу навыка
                # разбора («таблицы нет — разбор не запускается»), то есть
                # промпт спорил с навыком за один и тот же порядок работ.
                # Осталось ПРАВИЛО про параллель — оно про механику хода, а не
                # про то, чем эту работу делать.
                "### Несколько подзадач за один ход — не делай их подряд\n"
                "Если в просьбе несколько работ и вторая НЕ нуждается в результате первой — "
                "запускай их одновременно, а не по очереди. Зависимые (вторая работает по "
                "итогу первой) — только последовательно, и порядок бери из тела навыка, "
                "а не из желания ответить быстрее.\n"
                + ("Долгую работу можно отдать воркеру: `sessions_spawn` по шаблону выше, "
                   "со своим нонсом.\n" if bg_pool else "")
                + "В ответе сведи итоги КОРОТКО — по паре предложений на работу, без "
                "пересказа того, что система уже показала карточкой или таблицей."
            )
            # Ящики взводим ВСЕГДА, когда выдан пул нонсов: гейт по словам
            # запроса («фон/параллельно», связка «извлечение+анализ») молчаливо
            # терял итоги воркера, если агент распараллелил сам, а формулировка
            # пользователя в шаблон не попала. Неиспользованные ящики тихо
            # истекают по idle — цена холостого взвода ничтожна против потери
            # результата многоминутной работы.
            if bg_pool:
                from finengine.api import station_progress_ingest as _bg_ingest  # noqa: PLC0415
                from finengine.skills import run_orchestrator as _bg_runs  # noqa: PLC0415
                import asyncio as _bg_aio  # noqa: PLC0415
                bg_run_ids: list[dict] = []
                for _bg_n in bg_pool:
                    _q_mb = _bg_ingest.register(_bg_n)
                    _bg_aio.create_task(_bg_extract_mailbox(
                        session, _bg_n, _q_mb, project_id=project_id or "default"))
                    # Вторая очередь того же нонса → run kind="extract": фронт
                    # подписывается СУЩЕСТВУЮЩИМ /extract/runs/{id}/stream и
                    # рисует живую таблицу фонового прогона (#84 фронт-подводка).
                    _q_run = _bg_ingest.register(_bg_n)
                    _rid = _bg_runs.start_run(
                        (lambda n=_bg_n, qq=_q_run: _bg_run_relay(n, qq)),
                        kind="extract",
                        meta={"bg": True, "nonce": _bg_n,
                              "session_id": session.session_id,
                              "user_id": sess_owner},
                    )
                    bg_run_ids.append({"run_id": _rid, "nonce": _bg_n})
                logger.info("bg mailbox armed pool=%s runs=%s session=%s",
                            ",".join(bg_pool),
                            ",".join(r["run_id"][:8] for r in bg_run_ids),
                            session.session_id)
                # Анонс фронту: по этим run_id можно смотреть живой прогресс.
                yield {"type": "bg_runs", "runs": bg_run_ids}

        if _gate_fired:
            # Срез 29, Этап 4: «быстрый режим» для вопроса с числом уже
            # недостижим — гейт выше отработал ДО этого хода. Модели
            # остаётся процитировать её же результат, не пересчитывать.
            prompt_parts.append(
                "## Показатель уже посчитан каноническим конвейером\n\n"
                "Блок «[Tool result: financial_pipeline (OK)]» в Истории диалога ниже — "
                "это готовый typed-результат ЭТОГО хода, не прошлого. Отвечай ИЗ него: "
                "процитируй значение/статус словами, не пересчитывай и не ищи число заново "
                "по тексту документа. Если статус там «partial»/строка «missing» — скажи это "
                "честно, не подставляй значение из текста самостоятельно."
            )

        if history_text:
            prompt_parts.append(f"## История диалога:\n{history_text}")

        # Активный документ = тот, что открыт у пользователя в панели исходника.
        # В чате с несколькими документами команда без уточнения («проведи
        # извлечение») обязана бить в него — модель иначе берёт первый из списка.
        if active_doc_id and active_doc_id in (session.doc_ids or []):
            _adoc = None
            try:
                _adoc = get_store().get_document(active_doc_id)
            except Exception:  # noqa: BLE001 — подсказка не должна ронять ход
                pass
            _aname = (_adoc.company_name or _adoc.filename) if _adoc else active_doc_id
            prompt_parts.append(
                f"## Активный документ пользователя: «{_aname}» [doc_id: {active_doc_id}].\n"
                "Если запрос НЕ называет документ/компанию явно — работай именно с ним "
                "(извлечение, анализ, вопросы по цифрам). Другие документы бери только "
                "по явной просьбе."
            )

        if context and _gate_fired:
            # После успешного evidence-gate единственный числовой источник
            # для диспетчера — canonical tool_output в history. Живой smoke
            # 24.09.2026 показал, что при сохранённом сыром контексте модель
            # дополняет partial FormOutput ручной арифметикой из ОФР, несмотря
            # на промпт-запрет. Метаданные активного документа уже добавлены
            # выше; второй числовой канал здесь физически не выдаём.
            prompt_parts.append(
                "## Документ\n\n"
                "Сырой текст отчёта скрыт после срабатывания evidence-gate. "
                "Все допустимые числа этого хода находятся только в блоке "
                "`financial_pipeline` Истории диалога."
            )
        elif context:
            prompt_parts.append(f"## Документы (полный текст):\n{context}")
        elif session.doc_ids:
            # Attached docs exist but their text isn't ready yet (deferred-
            # processing model: OCR runs on the user's message via fe_idp_ocr,
            # not auto on upload). Expose doc_id + filename + status so the
            # agent processes on-demand instead of wrongly claiming "no docs".
            from finengine.ingestion.document_store import get_store as _gi
            _idx = _gi()
            doc_lines: list[str] = []
            diag: list[str] = []
            for d in session.doc_ids:
                meta = _idx.get_document(d)
                if not meta:
                    diag.append(f"{d[:8]}=no_meta")
                    continue
                if (owner_id is not None and getattr(meta, "owner_id", 0)
                        and meta.owner_id != owner_id):
                    diag.append(f"{d[:8]}=owner_mismatch")
                    continue
                from finengine.store.blobs import document_text_exists  # noqa: PLC0415

                ready = document_text_exists(d)
                diag.append(f"{d[:8]}={'txt' if ready else 'no_txt'}")
                tail = "" if ready else " — ещё распознаётся (OCR идёт с загрузки)"
                doc_lines.append(f"- {meta.company_name or meta.filename} [doc_id: {d}]{tail}")
            logger.warning("_build_context empty; doc_ids=%s : %s",
                           session.doc_ids, "; ".join(diag))
            if doc_lines:
                prompt_parts.append(
                    "## Прикреплённые документы:\n" + "\n".join(doc_lines)
                    + "\n\nТекст ещё распознаётся (OCR идёт с загрузки), но документы "
                    "ПРИКРЕПЛЕНЫ — НЕ говори, что их нет и не жди отдельно: работа с отчётом "
                    "сама дождётся готового текста. `fe_idp_ocr(doc_id)` — ТОЛЬКО фоллбэк, "
                    "если текст так и не распознался. doc_id бери из списка выше."
                )
            else:
                prompt_parts.append("## Документы: не загружены.")
        else:
            prompt_parts.append("## Документы: не загружены.")

        # 🔴 Р1: СВОИ ОПРЕДЕЛЕНИЯ ЧЕЛОВЕКА ПЕРЕЖИВАЮТ КОМНАТУ (24.08.2026).
        #
        # Файлы `--extra-metrics` и `--extra-calculated` живут в комнате станции,
        # а комната — двадцать минут. Следующим ходом человек говорит «поищи это
        # другими словами» или «на первом шаге ты написала A/B, а нужно
        # A/(B+C)» — и править нечего: ни описаний, ни формул уже нет нигде.
        #
        # Теперь станция кладёт принятое в конверт прогона (`own_metrics`,
        # `own_calculated`), конверт сохраняет хранилище исходов, а здесь мы
        # показываем его модели. Своего тула для этого не заводим: блок в
        # промпте дешевле и не добавляет модели решения, которое она не просила.
        свои = _own_definitions_for(session.doc_ids, tool_user_id)
        if свои:
            prompt_parts.append(свои)

        prompt_parts.append(f"## Вопрос пользователя:\n{message}")
        prompt_parts.append(
            "Отвечай на основе контекста и результатов инструментов. Если нужных данных в "
            "контексте нет — вызови подходящий инструмент (см. развилку выше / карту "
            "инструментов), НЕ отвечай из памяти и НЕ выдумывай. Указывай источник "
            "[документ] для фактов из отчёта. "
            # 🔴 РАЗВОДКА ДОБАВЛЕНА 23.08.2026. Здесь стояло безусловное «для
            # математических расчётов — инструмент Bash (Python)», и эта фраза
            # приезжала в КАЖДЫЙ ход, конкурируя ровно с тем, ради чего заведён
            # `--extra-calculated`: коэффициент, посчитанный питоном в уме
            # модели, не попадает ни в таблицу, ни в выгрузку, и проверить его
            # нечем. Прочая арифметика (дни между датами, доля от суммы в
            # разговоре) Bash-ом считается по-прежнему — запрещать её незачем.
            "Коэффициенты и показатели по ОТЧЁТНОСТИ считает навык расчёта, а не ты: "
            "нет такого коэффициента в реестре — подай свою формулу, так строка попадёт "
            "в таблицу и её можно будет проверить. Bash (Python) — для прочей арифметики."
        )

        full_prompt = "\n\n".join(prompt_parts)
        session.add("user", message)
        _obs.emit(
            user_id=tool_user_id, session_id=session.session_id,
            event="prompt_built",
            prompt=full_prompt,
            prompt_chars=len(full_prompt),
            sources=sources,
            history_turns=len(session.history),
        )

        # 3. Query via OpenClaw (Kimi K2.5 + thinking max + tools).
        # Final "thinking" line right before the LLM call — replaces any
        # parse-phase status with a generic "Думаю…" so the user sees the
        # transition into actual reasoning.
        yield {"type": "thinking", "text": "Думаю…"}
        full_response = ""
        try:
            from finengine.llm import tools as _tools_mod  # noqa: PLC0415
            ctx = _tools_mod.ToolContext(
                user_id=tool_user_id,
                project_id=project_id or None,
                session_id=session.session_id,
                role=role or "",
            )
            llm_usage: dict | None = None
            # When a "pre-rendered" tool (e.g. daily_news_digest) returns
            # canonical markdown, we stream that markdown verbatim and
            # suppress any subsequent LLM text — otherwise the model
            # rewrites/truncates the digest at its max_output budget.
            verbatim_done = False
            # Set when the model asks a clarifying question (ask_user) — relayed
            # to the client as a `clarify` event and recorded as the assistant
            # turn after the loop so the next turn's history shows the question.
            clarify_asked: dict | None = None
            # #98 C3-2: с ключом гейтвей-сессии петлю ведёт ГЕЙТВЕЙ (плагин
            # fe-tools проксирует тулы на бэк). Чипы/прогресс/история тулов
            # приходят в ран напрямую из /internal/tools/call, не из стрима.
            async for ev in llm.stream_events(
                prompt=full_prompt, tool_ctx=ctx,
                # After the canonical evidence gate succeeded the dispatcher
                # has exactly one job: phrase the already persisted typed
                # results.  Keeping the gateway tool loop enabled lets it call
                # read_source/extract again and reopen the forbidden raw-text
                # numeric path.  Run one tool-free synthesis turn instead.
                enable_tools=not _gate_fired,
                pinned_tool=pinned_tool or None,
                pinned_tool_args=pinned_tool_args or None,
                gateway_session_key=(gateway_session_key or None) if not _gate_fired else None,
            ):
                etype = ev.get("type")
                # Mirror EVERY LLM event into the trace log — full payload,
                # no truncation. Pure observability sink for debugging.
                _obs.emit(
                    user_id=tool_user_id, session_id=session.session_id,
                    event=f"llm.{etype}", payload=ev,
                )
                if etype == "text_delta":
                    if verbatim_done:
                        # LLM tried to add commentary after the pre-rendered
                        # digest — drop it. The markdown was the answer.
                        continue
                    text = ev.get("text", "")
                    full_response += text
                    yield {"type": "text_delta", "text": text}
                elif etype == "tool_call":
                    args = ev.get("args") or {}
                    name = ev.get("name", "?")
                    if name == "ask_user":
                        # Surfaced as a `clarify` event (option cards) below —
                        # don't render an «ask_user» tool chip.
                        continue
                    summary = _summarize_tool_args(name, args)
                    # Persist в session.history чтобы follow-up видел что
                    # модель уже сделала и не дёргала тул ещё раз.
                    session.add_tool_call(name, args)
                    yield {
                        "type": "tool_call",
                        "name": name,
                        "args_summary": summary,
                    }
                elif etype == "tool_output":
                    tool_name = ev.get("name", "?")
                    ok = bool(ev.get("ok", True))
                    output_full = ev.get("output_full") or ""
                    # Persist в историю — для follow-up'ов агенту нужны
                    # `factors[]`, `method_notes`, `aggregates`, иначе он
                    # перевызывает тул и теряет контекст.
                    session.add_tool_output(tool_name, ok, output_full)
                    yield {
                        "type": "tool_output",
                        "name": tool_name,
                        "ok": ok,
                    }
                    if not verbatim_done:
                        # Pre-rendered tools (digest/calendar/extract/analyze):
                        # stream the canonical markdown from the tool JSON and
                        # drop any LLM text after it (see _verbatim_render).
                        # full_response is REPLACED — kills the "Собираю…"
                        # preamble some models emit before the tool call.
                        _vtext, _vevents = _verbatim_render(tool_name, output_full)
                        for _ve in _vevents:
                            yield _ve
                        if _vtext:
                            full_response = _vtext
                            yield {"type": "text_delta", "text": _vtext}
                            verbatim_done = True
                elif etype == "clarify":
                    # Agent asked a clarifying question (ask_user). Relay as a
                    # `clarify` event → frontend renders option cards. The turn
                    # ends here; the user's pick arrives as the next message.
                    clarify_asked = {
                        "question": ev.get("question", ""),
                        "options": ev.get("options") or [],
                    }
                    yield {
                        "type": "clarify",
                        "question": clarify_asked["question"],
                        "options": clarify_asked["options"],
                    }
                elif etype == "tool_progress":
                    # Live progress from a long tool (e.g. full IR-calendar scan)
                    # — keeps the SSE alive (no idle-timeout) and shows status.
                    yield {
                        "type": "tool_progress",
                        "name": ev.get("name", "?"),
                        "text": ev.get("text", ""),
                    }
                elif etype in ("plan", "plan_step", "extract_start", "metric_row",
                               "extract_done", "extract_error", "text_segment_end"):
                    # Structured events for the chat «Прогресс» island and the
                    # live per-row metrics table — client-shaped, relay verbatim.
                    yield ev
                elif etype == "error":
                    yield {"type": "error", "error": ev.get("message", "unknown")}
                elif etype == "done":
                    llm_usage = ev.get("usage") or None
        except Exception as e:
            error_msg = f"Ошибка агента: {e}"
            yield {"type": "text_delta", "text": error_msg}
            full_response = error_msg
            import traceback as _tb
            _obs.emit(
                user_id=tool_user_id, session_id=session.session_id,
                event="agent_exception",
                error=str(e),
                error_class=e.__class__.__name__,
                traceback=_tb.format_exc(),
            )

        if clarify_asked and not full_response.strip():
            # Record the clarifying question as the assistant turn so the next
            # turn's history shows what was asked (the user's reply follows).
            _opts = ", ".join(clarify_asked.get("options") or [])
            full_response = (
                f"[Уточняющий вопрос] {clarify_asked.get('question', '')}"
                + (f" Варианты: {_opts}" if _opts else "")
            )
        session.add("assistant", full_response)
        _save_session(session)  # B: персист истории на диск (переживает рестарт)
        _obs.emit(
            user_id=tool_user_id, session_id=session.session_id,
            event="session_done",
            response=full_response,
            response_chars=len(full_response),
            usage=llm_usage,
        )

        # Persist token usage + cost so the project page can show
        # accumulated burn, and we have an audit trail when somebody
        # asks "why did this session cost $X". Done even on partial
        # responses — model still charged for what it generated.
        if llm_usage:
            try:
                from finengine.llm import traces as _traces  # noqa: PLC0415
                entry = _traces.log_turn(
                    user_id=tool_user_id,
                    project_id=project_id or None,
                    session_id=session.session_id,
                    usage=llm_usage,
                )
                project_total = _traces.project_totals(
                    user_id=tool_user_id, project_id=project_id or None,
                )
                yield {
                    "type": "usage",
                    "turn": entry,
                    "project_total": project_total,
                }
            except Exception:  # noqa: BLE001
                logger.exception("llm_traces: log_turn failed (non-fatal)")

        if sources:
            yield {"type": "sources", "sources": sources}

        yield {"type": "done"}

    def query(
        self,
        message: str,
        session_id: str = "",
        doc_ids: list[str] | None = None,
        owner_id: int | None = None,
        auth_user_id: int | None = None,
    ) -> dict:
        import asyncio
        loop = asyncio.new_event_loop()

        async def _collect():
            text = ""
            sess_id = ""
            sources = []
            async for event in self.query_stream(
                message, session_id, doc_ids, owner_id=owner_id, auth_user_id=auth_user_id,
            ):
                if event["type"] == "session_id":
                    sess_id = event["session_id"]
                elif event["type"] == "text_delta":
                    text += event["text"]
                elif event["type"] == "sources":
                    sources = event["sources"]
            return text, sess_id, sources

        text, sess_id, sources = loop.run_until_complete(_collect())
        loop.close()
        return {"answer": text, "session_id": sess_id, "sources": sources}


_agent: FinengineAgent | None = None


def get_agent() -> FinengineAgent:
    global _agent
    if _agent is None:
        _agent = FinengineAgent()
    return _agent
