"""Backend tools exposed to the OpenClaw agent.

Each tool has:
  - a JSON schema (`tool_def`) that the agent sees in the prompt
  - an async handler `run(args, ctx)` that we execute when the agent
    emits a function_call

Tools live here (not under skills/) because they're the **integration
seam** between OpenClaw's function-calling protocol and our existing
skill modules. The skills themselves stay pure — tools just wrap them
and shape the I/O for the agent.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """What a tool handler can use from the calling context — user id,
    project id, abort token, etc. Filled by the wrapper at call time."""
    user_id: int | str | None = None
    project_id: str | None = None
    session_id: str | None = None
    # Optional progress channel. stream_events sets this to an asyncio.Queue
    # before running a long tool; the handler put_nowait()s short status
    # strings, which stream_events drains into `tool_progress` SSE events —
    # keeps the connection alive (no idle-timeout) and shows live progress.
    progress: Any = None
    # User-selected analyst role (other/risks/baifp/die/cib/dgr) from the UI.
    # Роль, выбранная в интерфейсе. Состав показателей по ней собирает
    # навык извлечения (флаг --role), тул её больше не читает.
    # and it takes precedence over the tool's own `role` arg (UI is the
    # authoritative source of the role, not the model's guess).
    role: str = ""


class ToolAuthError(Exception):
    """Raised by handlers when ctx lacks the auth context they need."""


def _safe_path_segment(s: str) -> str:
    """Restrict a path segment to a tame charset so user_id / project_id
    can never traverse out of the namespace root."""
    return "".join(c for c in str(s) if c.isalnum() or c in ("-", "_")).strip("_-") or "_"


def require_user_ctx(ctx: ToolContext) -> tuple[str, str]:
    """Standard guard for tools that read or write user-scoped data.

    Returns (user_id_str, project_id_str) — both sanitized to be safe
    path segments. Raises ToolAuthError when either is missing.

    Usage:
        from finengine.llm.tools import require_user_ctx

        async def _h_my_tool(args, ctx):
            try:
                uid, pid = require_user_ctx(ctx)
            except ToolAuthError as e:
                return json.dumps({"error": str(e)})
            return json.dumps(sources.list_sources(user_id=uid, project_id=pid))

    Compliance: УЭК-АУА-24/25 — data access requires authorized user.
    """
    if ctx.user_id is None:
        raise ToolAuthError(
            "tool requires authorized user context (ctx.user_id is None)"
        )
    if not ctx.project_id:
        raise ToolAuthError(
            "tool requires project_id (call originated outside a project scope)"
        )
    return _safe_path_segment(ctx.user_id), _safe_path_segment(ctx.project_id)


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], Awaitable[str]]

    def to_openclaw(self) -> dict:
        """Serialize to OpenClaw `/v1/responses` tools[] entry."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


# ─────────────────────── handlers ──────────────────────


_FETCH_URL_MAX_REDIRECTS = 5


async def _h_fetch_url(args: dict, ctx: ToolContext) -> str:
    """Download a URL and return its text content.

    Atomic building block — agent composes complex flows (IR Calendar,
    deep-news-read) by combining fetch_url with tavily_search and its
    own reasoning. No HTML magic beyond a basic <script>/<style> strip
    + tag removal; if a page needs JS rendering the agent gets garbage
    and should fall back to tavily_search snippets.

    SSRF-защита (найдена и закрыта Срезом 7, тем же `url_safety.py`, что
    защищает `document_acquisition`): раньше `follow_redirects=True` шёл по
    любому адресу без проверки — localhost, приватные сети, облачные
    metadata-эндпоинты включительно. Редиректы теперь обрабатываются вручную,
    и КАЖДЫЙ hop (включая исходный URL) проходит ту же проверку схемы/
    userinfo/резолвинга, что и загрузчик PDF.
    """
    import httpx

    from finengine.skills.document_acquisition.page_text import html_to_text  # noqa: PLC0415
    from finengine.skills.document_acquisition.url_safety import (  # noqa: PLC0415
        UnsafeUrlError,
        validate_url_for_fetch,
    )

    url = str(args.get("url") or "").strip()
    if not url:
        return json.dumps({"error": "url required"}, ensure_ascii=False)
    if not (url.startswith("http://") or url.startswith("https://")):
        return json.dumps({"error": "url must start with http:// or https://"}, ensure_ascii=False)
    max_chars = int(args.get("max_chars") or 80_000)
    max_chars = max(1_000, min(max_chars, 200_000))

    current_url = url
    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=False,
            headers={"User-Agent": "FA-CLAW/agent fetch_url (Mozilla/5.0)"},
        ) as client:
            resp = None
            for _hop in range(_FETCH_URL_MAX_REDIRECTS + 1):
                try:
                    validate_url_for_fetch(current_url)
                except UnsafeUrlError as e:
                    return json.dumps({"error": f"url rejected: {e}"}, ensure_ascii=False)
                resp = await client.get(current_url)
                if not resp.is_redirect:
                    break
                location = resp.headers.get("location")
                if not location:
                    return json.dumps({"error": f"{current_url}: redirect without Location"}, ensure_ascii=False)
                current_url = str(httpx.URL(current_url).join(location))
            else:
                return json.dumps(
                    {"error": f"{url}: too many redirects (>{_FETCH_URL_MAX_REDIRECTS})"}, ensure_ascii=False
                )
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as e:
        return json.dumps({"error": f"fetch failed: {e}"}, ensure_ascii=False)

    # Basic-but-good-enough HTML → text. Keep it cheap — no BeautifulSoup
    # pull-in just for this. Тот же код, что у атома-загрузки цикла.
    s = html_to_text(html)
    truncated = len(s) > max_chars
    if truncated:
        s = s[:max_chars]

    return json.dumps(
        {
            "url": str(resp.url),
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "text": s,
            "truncated": truncated,
            "full_length": len(s) + (1 if truncated else 0),
        },
        ensure_ascii=False,
    )


async def _h_list_sources(args: dict, ctx: ToolContext) -> str:
    """Return list of files attached as Sources to the current project."""
    from finengine.skills import sources as _sources
    try:
        uid, pid = require_user_ctx(ctx)
    except ToolAuthError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    items = _sources.list_sources(user_id=uid, project_id=pid)
    rows = [{"file_id": m.file_id, "filename": m.filename,
             "content_type": m.content_type, "size": m.size,
             "uploaded_at": m.uploaded_at, "text_status": m.text_status}
            for m in items]
    if rows:
        return json.dumps(rows, ensure_ascii=False)
    # 🔴 ПУСТОЙ СПИСОК — НЕ «ДОКУМЕНТОВ НЕТ» (20.08.2026). «Источники» и
    # ДОКУМЕНТЫ — два разных хранилища: приложенный к чату отчёт лежит во
    # втором, и этот тул его не видит НИКОГДА. Ход по Северстали: модель
    # позвала `list_sources`, получила пустой массив, решила, что читать
    # документ нечем, и выписала числа глазами из обрезка текста в своём
    # контексте. Голый `[]` она прочитала как «источников нет вообще».
    return json.dumps({
        "sources": [],
        "note": ("в «Источниках» этого чата пусто. Отчёт, приложенный к чату, "
                 "лежит в ДОКУМЕНТАХ — этот инструмент его не показывает. "
                 "Показатели по отчёту даёт извлечение (навык работы с "
                 "отчётом), а не чтение источников."),
    }, ensure_ascii=False)


async def _h_read_source(args: dict, ctx: ToolContext) -> str:
    """Return extracted text of a project source by file_id."""
    from finengine.skills import sources as _sources
    try:
        uid, pid = require_user_ctx(ctx)
    except ToolAuthError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    file_id = str(args.get("file_id") or "").strip()
    if not file_id:
        return json.dumps({"error": "file_id required"}, ensure_ascii=False)
    max_chars = int(args.get("max_chars") or 80_000)
    max_chars = max(1_000, min(max_chars, 200_000))
    text = _sources.read_source_text(user_id=uid, project_id=pid, file_id=file_id, max_chars=max_chars)
    if text is None:
        meta = _sources.get_source_meta(user_id=uid, project_id=pid, file_id=file_id)
        return json.dumps(
            {"error": "text not available", "meta": meta.to_dict() if meta else None},
            ensure_ascii=False,
        )
    meta = _sources.get_source_meta(user_id=uid, project_id=pid, file_id=file_id)
    return json.dumps(
        {"file_id": file_id,
         "filename": meta.filename if meta else "",
         "text": text,
         "truncated": len(text) >= max_chars},
        ensure_ascii=False,
    )


async def _h_factor_analysis(args: dict, ctx: ToolContext) -> str:
    """Run factor-analysis decomposition on user-uploaded plan/fact xlsx.

    Args:
        plan_file_id / fact_file_id: ids из list_sources. Если не указаны —
            пробуем найти автоматически в sources по эвристике имени
            (содержит "план"/"plan" vs "факт"/"fact").
        nomenclature_file_id: optional справочник.
        preset: "default" | "full_tz" | "revenue_only" | None.
        products: список кодов продуктов для фильтра (None = все).
        factors: явный список факторов (переопределяет preset).
        period_label: человеческое имя периода ("Q1 2026" и т.п.).
    """
    from finengine.skills import sources as _sources

    try:
        uid, pid = require_user_ctx(ctx)
    except ToolAuthError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    src_list = _sources.list_sources(user_id=uid, project_id=pid)
    by_id = {m.file_id: m for m in src_list}
    plan_id = str(args.get("plan_file_id") or "").strip()
    fact_id = str(args.get("fact_file_id") or "").strip()
    nom_id = str(args.get("nomenclature_file_id") or "").strip()

    # Auto-detect by filename heuristic when not provided
    if not plan_id or not fact_id:
        import re as _re_auto
        # Word-boundary matching, чтобы "fact" не зацепил наш собственный
        # артефакт "factor_analysis_*.xlsx" (где fact это начало слова factor).
        # Кириллица в \b работает; для безопасности используем разделители
        # вокруг.
        SEP = r"[^A-Za-zА-Яа-яЁё0-9]"
        def _has_word(text: str, *words: str) -> bool:
            for w in words:
                if _re_auto.search(rf"(^|{SEP}){_re_auto.escape(w)}({SEP}|$)", text):
                    return True
            return False
        for m in src_list:
            ln = m.filename.lower()
            if not ln.endswith((".xlsx", ".xls")):
                continue
            # Skip наши же артефакты — они начинаются с "factor_analysis_"
            if ln.startswith("factor_analysis"):
                continue
            if not plan_id and _has_word(ln, "план", "plan"):
                plan_id = m.file_id
            elif not fact_id and _has_word(ln, "факт", "fact"):
                fact_id = m.file_id
            elif not nom_id and _has_word(
                ln, "номенклатура", "номенкл", "nomenclature", "nomencl", "справочник", "справ"
            ):
                nom_id = m.file_id

    if not plan_id or not fact_id:
        return json.dumps(
            {"error": "не нашёл план/факт xlsx — укажи plan_file_id и fact_file_id "
                      "явно либо переименуй файлы (план/факт в имени)"},
            ensure_ascii=False,
        )
    if plan_id not in by_id or fact_id not in by_id:
        return json.dumps({"error": "указанный file_id не найден в sources"}, ensure_ascii=False)

    # Разбор книг требует ПУТИ на диске (openpyxl берёт файл, не поток), а оригиналы источников лежат в
    # каталоге ouroboros (ADR-0012): `source_path` кладёт байты во временный каталог на время разбора и
    # убирает его на любом выходе.
    from contextlib import ExitStack  # noqa: PLC0415

    with ExitStack() as _уборка:
        plan_path = _уборка.enter_context(_sources.source_path(uid, pid, plan_id))
        fact_path = _уборка.enter_context(_sources.source_path(uid, pid, fact_id))
        nom_path = _уборка.enter_context(_sources.source_path(uid, pid, nom_id)) if nom_id else None
        if plan_path is None or fact_path is None:
            return json.dumps({"error": "xlsx файл не найден в хранилище"},
                              ensure_ascii=False)
        return await _factor_analysis_on_paths(
            args, by_id, uid, pid, plan_id, fact_id, nom_id,
            plan_path, fact_path, nom_path)


async def _factor_analysis_on_paths(args, by_id, uid, pid, plan_id, fact_id, nom_id,
                                    plan_path, fact_path, nom_path) -> str:
    """Продолжение факторного анализа, когда книги уже лежат файлами.

    Отдельная функция, а не тело `with`: временные файлы обязаны убраться
    ровно тогда, когда разбор кончился, и держать под `with` весь остаток
    (сборку книги и сохранение артефакта) значило бы держать их лишние секунды.
    Вложенный `def` запрещён `docs/rules/coding_standards.md`.
    """

    from finengine.skills import sources as _sources  # noqa: PLC0415
    from finengine.skills.factor_analysis import analyze  # noqa: PLC0415

    scope: dict = {}
    if args.get("preset"):
        scope["preset"] = str(args["preset"])
    if args.get("products"):
        scope["products"] = list(args["products"])
    if args.get("factors"):
        scope["factors"] = list(args["factors"])

    try:
        report = analyze(
            plan_path=plan_path,
            fact_path=fact_path,
            nomenclature_path=nom_path,
            scope=scope or None,
            period_label=str(args.get("period_label") or ""),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("factor_analysis crashed")
        return json.dumps({"error": f"factor_analysis crashed: {e}"}, ensure_ascii=False)

    d = report.to_dict()
    d["plan_filename"] = by_id[plan_id].filename
    d["fact_filename"] = by_id[fact_id].filename
    if nom_id and nom_id in by_id:
        d["nomenclature_filename"] = by_id[nom_id].filename

    # Build xlsx artifact (multi-sheet report) and save it as a project Source
    # so юзер сможет открыть/скачать через панель «Источники → Документы».
    try:
        from finengine.skills.factor_analysis.excel_report import build_report_xlsx
        import tempfile
        import os
        period_safe = (str(args.get("period_label") or "FA")
                       .replace("/", "_").replace(" ", "_"))
        artifact_name = f"factor_analysis_{period_safe}.xlsx"
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            build_report_xlsx(d, tmp_path)
            with open(tmp_path, "rb") as f:
                content = f.read()
        finally:
            os.unlink(tmp_path)
        meta = await _sources.save_upload(
            user_id=uid, project_id=pid,
            filename=artifact_name, content=content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        d["xlsx_artifact"] = {
            "file_id": meta.file_id,
            "filename": meta.filename,
            "size": meta.size,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("factor_analysis xlsx generation failed")
        d["xlsx_artifact_error"] = str(e)

    return json.dumps(d, ensure_ascii=False, default=str)


async def _h_get_daily_registry(args: dict, ctx: ToolContext) -> str:
    """Return the bundled IR Daily registry (78 companies, 11 macro
    indicators, 7 regulators, trusted sources). Static config — same
    payload for every caller. The agent uses this to know what to
    monitor; the per-user watchlist is what to *prioritize*."""
    try:
        import yaml
        from pathlib import Path as _P
        path = _P(__file__).parent.parent / "skills" / "ir_data" / "daily_registry.yaml"
        with path.open(encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        return json.dumps(doc, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"registry load failed: {e}"}, ensure_ascii=False)


async def _h_get_watchlist(args: dict, ctx: ToolContext) -> str:
    """Return current watchlist (companies the user is monitoring).
    On first call for a fresh project, seeds from the bundled registry's
    `companies` block — so the agent always has something to work with."""
    from finengine.skills import watchlist as _wl
    try:
        uid, pid = require_user_ctx(ctx)
    except ToolAuthError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    data = _wl.load(user_id=uid, project_id=pid)
    return json.dumps(
        {"ru": data["ru"], "world": data["world"],
         "count": len(data["ru"]) + len(data["world"])},
        ensure_ascii=False,
    )


async def _h_update_watchlist(args: dict, ctx: ToolContext) -> str:
    """Add or remove a company from the per-project watchlist.

    Args:
        action: "add" | "remove"
        country: "ru" | "world" (required for add, ignored for remove)
        name: company display name (matched case-insensitively for remove)
    """
    from finengine.skills import watchlist as _wl
    try:
        uid, pid = require_user_ctx(ctx)
    except ToolAuthError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    action = str(args.get("action") or "").strip().lower()
    name = str(args.get("name") or "").strip()
    if action == "add":
        country = str(args.get("country") or "").strip().lower()
        ok, msg = _wl.add(user_id=uid, project_id=pid, country=country, name=name)
    elif action == "remove":
        ok, msg = _wl.remove(user_id=uid, project_id=pid, name=name)
    else:
        return json.dumps(
            {"error": f"unknown action {action!r}; expected 'add' or 'remove'"},
            ensure_ascii=False,
        )
    return json.dumps({"changed": ok, "message": msg}, ensure_ascii=False)


async def _h_fe_skill_editor(args: dict, ctx: ToolContext) -> str:
    """#93 (S3 v1): редактор навыков — просьба текстом → правка кода навыка
    (glm-5.2) → гейт (компиляция) → готовый патч в артефакты + на диск.
    Активация контролируемая (git+деплой), НЕ мгновенная — так и отвечай."""
    uid = str(getattr(ctx, "user_id", "") or "").strip()
    if not uid.isdigit():
        return json.dumps({"error": "нет пользователя в контексте — тул недоступен"},
                          ensure_ascii=False)
    from finengine.skills.scenarios import skill_editor as _se  # noqa: PLC0415
    action = str(args.get("action") or "propose").strip().lower()
    if action == "list":
        return json.dumps({"skills": {k: v[1] for k, v in _se.SKILL_FILES.items()},
                           "note": "action=propose с target и request — предложить правку"},
                          ensure_ascii=False)
    if ctx.progress is not None:
        try:
            ctx.progress.put_nowait("редактор навыков: готовлю правку (до минуты)…")
        except Exception:  # noqa: BLE001
            pass
    res = await _se.propose_patch(
        user_id=int(uid),
        target=str(args.get("target") or "").strip(),
        request=str(args.get("request") or "").strip(),
    )
    return json.dumps(res, ensure_ascii=False)


async def _h_fe_scenario_settings(args: dict, ctx: ToolContext) -> str:
    """#88: агент правит настройки автоматизаций (сценариев) из чата.

    action=list — сценарии пользователя (id/имя/kind/расписание/params).
    action=update — точечный патч: schedule, params (мердж; null снимает ключ),
    paused. Owner-scoped: только сценарии текущего пользователя. Изменения
    применяются при СЛЕДУЮЩЕМ запуске (кнопкой на сайте или по расписанию)."""
    from finengine.skills.scenarios import store as _scn_store
    # Сценарии привязаны к ПОЛЬЗОВАТЕЛЮ, не к проекту — project_id не требуем
    # (require_user_ctx валил тул в чатах без project_id, модель зацикливалась).
    uid = str(getattr(ctx, "user_id", "") or "").strip()
    if not uid.isdigit():
        return json.dumps({"error": "нет пользователя в контексте — тул недоступен"},
                          ensure_ascii=False)
    action = str(args.get("action") or "list").strip().lower()
    if action == "list":
        rows = [{
            "scenario_id": s.id, "name": s.name, "kind": s.kind,
            "schedule": {"freq": s.freq, "at_time": s.at_time, "dows": s.dow_list},
            "paused": bool(s.paused), "params": s.params_dict,
            "instructions": s.instructions,
        } for s in _scn_store.list_for_user(int(uid))]
        return json.dumps({"scenarios": rows, "note": (
            "params календаря: back_days/fwd_days (окно в ДНЯХ, 1..120), event_groups (reporting/investor_day/other). "
            "Расписание меняй только по явной просьбе пользователя."
        )}, ensure_ascii=False)
    if action != "update":
        return json.dumps({"error": f"unknown action {action!r}; list|update"}, ensure_ascii=False)
    sid = str(args.get("scenario_id") or "").strip()
    scn = _scn_store.get(sid) if sid else None
    if scn is None or scn.user_id != int(uid):
        return json.dumps({"error": "сценарий не найден (сначала action=list и возьми scenario_id)"},
                          ensure_ascii=False)
    fields: dict = {}
    sched = args.get("schedule")
    if isinstance(sched, dict):
        freq = str(sched.get("freq") or scn.freq)
        if freq not in _scn_store.FREQS:
            return json.dumps({"error": f"freq {freq!r} не из {sorted(_scn_store.FREQS)}"}, ensure_ascii=False)
        fields["freq"] = freq
        fields["at_time"] = str(sched.get("at_time") or scn.at_time)
        if sched.get("dows") is not None:
            fields["dows"] = sched.get("dows")
    if args.get("paused") is not None:
        fields["paused"] = bool(args.get("paused"))
    if args.get("instructions") is not None:
        # Инструкция сценария: дайджест — фокус-фильтр classify-стадии («только
        # европейский рынок»); календарь — указания LLM-экстрактору событий.
        fields["instructions"] = str(args.get("instructions"))[:4000]
    params = args.get("params")
    if isinstance(params, dict):
        # #91: мердж + валидация по единой схеме — паритет с PATCH /scenarios.
        from finengine.skills.scenarios import schema as _scn_schema  # noqa: PLC0415
        merged = dict(scn.params_dict)
        for k, v in params.items():
            if v is None:
                merged.pop(k, None)
                continue
            norm, err = _scn_schema.validate_param(scn.kind, k, v)
            if err:
                return json.dumps({"error": err}, ensure_ascii=False)
            merged[k] = norm if norm is not None else v
        fields["params"] = merged
    if not fields:
        return json.dumps({"error": "нечего менять: передай schedule, params или paused"},
                          ensure_ascii=False)
    upd = _scn_store.update(sid, **fields)
    # #92: сохранили инструкцию → компилятор переводит её в диф параметров и
    # честный отчёт (агент перескажет пользователю, что применилось, а что нет).
    compiled_note = ""
    if "instructions" in fields and upd is not None:
        from finengine.skills.scenarios import compiler as _scn_compiler  # noqa: PLC0415
        rep = await _scn_compiler.compile_instructions(upd, upd.instructions)
        new_params = dict(upd.params_dict)
        new_params.update(rep.get("params_diff") or {})
        if rep.get("note"):
            new_params["_compiled_note"] = compiled_note = rep["note"]
        upd = _scn_store.update(sid, params=new_params) or upd
    return json.dumps({
        "updated": True, "scenario_id": sid, "name": upd.name if upd else scn.name,
        "compiled": compiled_note,
        "instructions": (upd.instructions if upd else scn.instructions) or "",
        "schedule": {"freq": upd.freq, "at_time": upd.at_time, "dows": upd.dow_list} if upd else None,
        "params": upd.params_dict if upd else None,
        "paused": bool(upd.paused) if upd else None,
        "note": "настройки сохранены — применятся при следующем запуске автоматизации",
    }, ensure_ascii=False)


async def _h_daily_news_digest(args: dict, ctx: ToolContext) -> str:
    """Run the Daily News ETL pipeline and return its markdown.

    Pipeline does ALL the work (collect RSS / dedup / rule-filter / one
    LLM batch / render). Agent just calls this and prints the result —
    no multi-step tavily_search dance needed.
    """
    fresh = bool(args.get("fresh", False))
    try:
        from finengine.skills.news.pipeline import (
            get_cached_digest, run_daily_news_pipeline, settings_for_user,
        )
        # 🔴 НАСТРОЙКИ ВЛАДЕЛЬЦА, А НЕ УМОЛЧАНИЯ ТУЛА (22.08.2026).
        # Здесь стояло `args.get("since_hours", 48)` и `args.get("top_n", 60)`.
        # Кнопка «Запустить» идёт через раннер сценария и читает его `params`,
        # а чат подставлял свои числа — то есть один и тот же дайджест собирался
        # ПО-РАЗНОМУ в зависимости от того, откуда его позвали. Живой прогон
        # админа 22.08: окно 48 часов вместо сохранённых восьми, 3911 новостей
        # вместо обычных ~900; и потолок кандидатов 60 вместо настроенных 100,
        # потому что явно переданное значение сильнее настройки.
        #
        # Порядок: что попросили в вызове → что сохранил владелец → умолчание.
        _own = settings_for_user(getattr(ctx, "user_id", None))
        since_hours = int(args.get("since_hours") or _own.get("since_hours") or 48)
        top_n = args.get("top_n") or _own.get("top_n")
        top_n = int(top_n) if top_n else None
        _groups = args.get("source_groups") or _own.get("source_groups")
        _macro = str(args.get("macro_mode") or _own.get("macro_mode") or "both")
        if not fresh:
            # Владелец тот же, что и у свежего прогона ниже: иначе чтение кэша
            # и запуск расходятся, и пользователь получает ЧУЖОЙ выпуск —
            # собранный по чужому окну и чужим источникам.
            cached = get_cached_digest(user_id=getattr(ctx, "user_id", None))
            if cached is not None:
                cached["from_cache"] = True
                return json.dumps(cached, ensure_ascii=False, default=str)
        # Прогон идёт 5-7 минут; без этих сообщений чат молчит всё это время и
        # выглядит зависшим (жалоба владельца 24.08). Канал тот же, каким
        # календарь шлёт свои стадии.
        def _tick(text: str) -> None:
            q = ctx.progress
            if q is not None:
                q.put_nowait(text)

        result = await run_daily_news_pipeline(
            since_hours=since_hours, top_n_candidates=top_n,
            progress=_tick,
            source_groups=_groups if isinstance(_groups, dict) else None,
            macro_mode=_macro if _macro in ("both", "week_only", "full") else "both",
            # Pipeline applies the user's source overlay (their adds +
            # disables on top of the global catalogue) when user_id is
            # present in the tool context.
            user_id=getattr(ctx, "user_id", None),
        )
        result["from_cache"] = False
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"pipeline failed: {e}"}, ensure_ascii=False)


async def _h_news_lookup(args: dict, ctx: ToolContext) -> str:
    """Ответ по УЖЕ СОБРАННЫМ новостям — без похода в интернет.

    Появился 17.08.2026 после живого хода: владелец спросил про новость из
    только что собранного выпуска, агент ответил «не нашёл в дайджесте» и
    девять раз позвал веб-поиск, пока ход не оборвал ограничитель. Прочитать
    собственный выпуск ему было НЕЧЕМ — инструмента не существовало, хотя
    данные лежали в базе.
    """
    import asyncio as _aio  # noqa: PLC0415 — так же берут его ниже по файлу

    from finengine.skills.news import db as _news_db

    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "нужен query — о чём спрашиваем"},
                          ensure_ascii=False)
    days = max(1, min(30, int(args.get("days") or 7)))
    limit = max(1, min(10, int(args.get("limit") or 5)))
    try:
        # Схема создаётся здесь же: инструмент может оказаться ПЕРВЫМ, кто
        # трогает базу новостей — пользователь спрашивает про новости раньше,
        # чем в этом окружении хоть раз собирали дайджест. Без этого агент
        # получал «no such table: digest_items» вместо внятного «ничего нет».
        # Вызов идемпотентный (CREATE TABLE IF NOT EXISTS).
        await _aio.to_thread(_news_db.init_schema)
        rows = await _aio.to_thread(
            _news_db.search_digest_items, query, days=days, limit=limit)
    except Exception as e:  # noqa: BLE001 — база может быть не создана
        log.warning("news_lookup failed: %s", e)
        return json.dumps({"error": f"поиск по дайджесту не удался: {e}"},
                          ensure_ascii=False)
    if not rows:
        # Явный и ОКОНЧАТЕЛЬНЫЙ ответ: повторять запрос бессмысленно, и это
        # сказано словами — модель девять раз повторяла отказ поиска именно
        # потому, что не понимала его окончательности.
        return json.dumps({
            "found": 0,
            "note": (f"В собранных новостях за {days} дн. по запросу «{query}» "
                     "ничего нет. Повторять этот вызов с тем же запросом "
                     "бесполезно — либо переформулируй короче (одно-два слова "
                     "из заголовка), либо скажи пользователю, что новости по "
                     "этой теме в выпуске не было."),
        }, ensure_ascii=False)
    return json.dumps({"found": len(rows), "items": rows},
                      ensure_ascii=False, default=str)


async def _h_web_search(args: dict, ctx: ToolContext) -> str:
    """Run a Tavily search and return a compact JSON list of hits."""
    from finengine.skills import web_search as _ws

    if not _ws.is_enabled():
        return json.dumps({
            "error": "Tavily не настроен. Добавьте FINENGINE_TAVILY_API_KEY в .env."
        }, ensure_ascii=False)

    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query required"}, ensure_ascii=False)
    max_results = int(args.get("max_results") or 5)

    try:
        results = await _ws.web_search(query, max_results=max_results)
    except Exception as e:  # noqa: BLE001
        log.warning("web_search failed: %s", e)
        return json.dumps({"error": f"search failed: {e}"}, ensure_ascii=False)

    return json.dumps(
        [{"title": r.title, "url": r.url, "snippet": r.content[:500]} for r in results],
        ensure_ascii=False,
    )


_EVENT_MONTHS = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
                 7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}


def _fmt_event_date(iso: str) -> str:
    """`2026-08-04` → `4 авг`; из другого года — `4 авг 2027`.

    Год опускался всегда: «окно календаря ±14 дней, он одинаков во всех
    строках — четыре лишних знака в каждой». С 25.08.2026 окно задаётся до 120
    дней в каждую сторону, то есть строки могут разойтись по годам, и «4 авг»
    станет двусмысленным. Показываем год только когда он не текущий — экономия
    знаков остаётся там, где она была верна.
    """
    if len(iso) < 10:
        return iso
    try:
        y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    except ValueError:
        return iso
    from datetime import date as _date
    # 🔴 НЕРАЗРЫВНЫЙ ПРОБЕЛ (25.08.2026). Разметка таблицы верна, но колонку
    # «Дата» браузер сжимает до минимума, и «24 авг» ломалось на две строки —
    # «24» сверху, «авг» снизу. Владелец прислал снимок: строки таблицы стали
    # вдвое выше, читать календарь неудобно. Выравнивание markdown ширину не
    # задаёт, поэтому держим дату вместе на уровне текста.
    nb = "\u00a0"
    out = f"{d}{nb}{_EVENT_MONTHS.get(m, '')}"
    return out if y == _date.today().year else f"{out}{nb}{y}"


def _calendar_settings_for_user(user_id: int | None) -> dict:
    """Окно календаря, сохранённое владельцем на странице автоматизаций."""
    if user_id is None:
        return {}
    from finengine.skills.scenarios import store as _scn_store

    for scn in _scn_store.list_for_user(user_id):
        if scn.kind == "ir_calendar":
            return dict(scn.params_dict or {})
    return {}


async def _h_ir_calendar_run(args: dict, ctx: ToolContext) -> str:
    """Trigger the IR Calendar scan — hybrid ±14d multi-source (IR sites +
    Finnhub + e-disclosure + smart-lab). Runs the scan as a run_orchestrator
    BACKGROUND task (so it survives client disconnect AND persists the artifact),
    streaming progress to the chat via ctx.progress so the SSE doesn't idle-time-
    out during the multi-minute scan. Returns a status summary the agent renders.

    Modes:
      - `companies` (list of names) → scan only those (targeted, fast).
      - else → FULL scan of ALL companies («запусти календарь» = весь список)."""
    from finengine.skills import ir_calendar, run_orchestrator
    from finengine.store import artifacts as _art

    names = args.get("companies")
    names = names if isinstance(names, list) and names else None
    limit = args.get("limit")
    try:
        limit_i = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        limit_i = None

    uid, pid = ctx.user_id, ctx.project_id

    # 🔴 ОКНО ДОЕЗЖАЕТ ДО СКАНА (25.08.2026). Просьба «собери календарь за 1
    # день» не срабатывала: у инструмента не было полей `back_days`/`fwd_days`
    # ВОВСЕ, и обработчик звал поток без окна — модель физически не могла
    # выразить период. В логе прода это видно по запросам к Finnhub:
    # `from=2026-08-11&to=2026-09-08` при просьбе про один день, то есть ±14.
    #
    # Возможность была: `build_calendar_stream` окно принимает, и в сценарии
    # ручка работает (замер 21.08: ±1 → 2 события, ±120 → 8 событий с
    # разбросом -113…+90). Не доезжала она ровно до чата.
    #
    # Порядок тот же, что у дайджеста: вызов → сценарий владельца → умолчание.
    _cal = _calendar_settings_for_user(uid)

    def _win(key: str):
        v = args.get(key) or _cal.get(key)
        try:
            return max(1, min(120, int(v))) if v else None
        except (TypeError, ValueError):
            return None

    back_days, fwd_days = _win("back_days"), _win("fwd_days")

    async def _runner():
        evs: list[dict] = []
        ld: dict = {}
        async for ev in ir_calendar.build_calendar_stream(
                limit=limit_i, names=names,
                back_days=back_days, fwd_days=fwd_days):
            if ev.get("type") == "events":
                evs.extend(ev.get("events", []))
            elif ev.get("type") == "done":
                ld = ev
            yield ev
        if uid is not None and pid and evs:
            meta = _art.save_artifact(
                user_id=uid, project_id=pid, kind="ir_calendar",
                payload={"events": evs, "lookahead_days": fwd_days or 14},
                title=f"IR Calendar — {len(evs)} событий",
                summary=(f"{ld.get('total_companies', 0)} компаний, окно "
                         f"−{back_days or 14}/+{fwd_days or 14} дн."),
                params={"limit": limit_i, "companies": names, "via": "agent_tool"},
                counts={"events": len(evs), "companies": ld.get("total_companies", 0)},
            )
            yield {"type": "artifact", "run_id": meta.run_id, "kind": meta.kind}

    run_id = run_orchestrator.start_run(
        _runner, kind="ir_calendar",
        meta={"user_id": uid, "project_id": pid, "via": "chat"},
    )

    def _progress(text: str) -> None:
        q = ctx.progress
        if q is not None:
            try:
                q.put_nowait(text)
            except Exception:  # noqa: BLE001
                pass

    events: list[dict] = []
    done: dict = {}
    artifact_run_id = None
    async for ev in run_orchestrator.subscribe(run_id, kind="ir_calendar"):
        t = ev.get("type")
        if t == "started":
            # Окно НАСТОЯЩЕЕ, а не зашитое: с 25.08 его задаёт вызов или
            # сценарий владельца, и «±14» в сообщении врало бы на просьбе
            # «за 1 день». Время названо честно: работа пропорциональна числу
            # КОМПАНИЙ, а не ширине окна — узкое окно скан не ускоряет.
            _n = ev.get("companies", 0)
            _wb, _wf = ev.get("window_back", 14), ev.get("window_fwd", 14)
            _mins = max(1, round(_n * 13 / 60))
            _progress(
                f"Сканирую {_n} компаний, окно −{_wb}/+{_wf} дн. "
                f"(~{_mins} мин: каждую страницу читает модель)…"
            )
        elif t == "source_done":
            _progress(f"Источник {ev.get('source')}: +{ev.get('events', 0)} событий")
        elif t == "company_done":
            # Раньше тик был только на КОМПАНИИ С СОБЫТИЯМИ: на узком окне их
            # почти нет, и чат замолкал на двадцать минут — владелец 25.08
            # написал «поставил, не увидел ответа». Показываем ход обхода
            # каждые десять компаний, чтобы ожидание было видно.
            _d, _tot = ev.get("done", 0), ev.get("total", 0)
            if _d % 10 == 0 or _d == _tot:
                _progress(f"…обработано {_d} из {_tot}, найдено {len(events)} событий")
        elif t == "events":
            events.extend(ev.get("events", []))
        elif t == "done":
            done = ev
            _progress(
                f"Готово: {done.get('total_companies', 0)} компаний · "
                f"{len(events)} событий · не удалось {done.get('fetch_failed', 0)}"
            )
        elif t == "artifact":
            artifact_run_id = ev.get("run_id")

    # Pre-render the FULL table as markdown so agent.py streams it VERBATIM.
    # Letting the LLM render it from the events array led to truncation
    # ("показал 7 из 34") + an "уточните запрос" cop-out — the agent must NOT
    # rewrite this. (Same pattern as daily_news_digest.)
    n_scanned = done.get("total_companies", 0)
    n_failed = done.get("fetch_failed", 0)
    failed_companies = done.get("failed_companies", [])
    n_with = len({e.get("company") for e in events})
    status = (
        f"**Статус:** просканировано {n_scanned} · с событиями {n_with} · "
        f"не удалось {n_failed}"
        + (f" ({', '.join(failed_companies)})" if failed_companies else "")
        + f" · у остальных нет событий в окне −{back_days or 14}/+{fwd_days or 14} дн."
    )
    if events:
        # Порядок колонок и формат даты — по разбору владельца 18.08.2026:
        # «хочется видеть дату и компанию, какая публикует отчётность»,
        # «дата опять поехала», «события можно поплотнее».
        #
        # Было: Дата | Событие | Компания | Источник, дата сырая «2026-08-04»,
        # компания третьей — то есть главное (кто и когда) стояло по краям, а
        # середину занимало описание. Стало: КОМПАНИЯ сразу за датой, дата
        # человеческая, ссылка стрелкой вместо слова «ссылка» — четыре знака
        # против семи в каждой строке.
        #
        # Выравнивание задаётся явно (`:---`): фронт прижимает вправо все
        # колонки кроме первой (правило писалось под числовые таблицы), и
        # текстовый календарь от этого разъезжается.
        rows = ["| Дата | Компания | Событие | Ист. |",
                "|:---|:---|:---|:---:|"]
        for e in sorted(events, key=lambda x: (str(x.get("date", "")), str(x.get("company", "")))):
            conf = " ✓✓" if len(e.get("sources", [])) >= 2 else ""
            desc = (e.get("raw_excerpt") or e.get("event_type") or "событие").replace("|", "/")
            rows.append(
                f"| {_fmt_event_date(str(e.get('date', '')))} | "
                f"**{e.get('company', '')}**{conf} | {desc} | "
                f"[↗]({e.get('source_url', '')}) |"
            )
        markdown = status + "\n\n## Календарь корпоративных событий\n\n" + "\n".join(rows)
    else:
        markdown = status + (
            f"\n\nСобытий в окне −{back_days or 14}/+{fwd_days or 14} дн. не найдено."
        )

    return json.dumps(
        {
            "markdown": markdown,  # agent streams this verbatim — DO NOT rewrite
            "companies_scanned": n_scanned,
            "companies_failed": n_failed,
            "failed_companies": failed_companies,
            "companies_with_events": n_with,
            "events_count": len(events),
            "by_source": done.get("by_source", {}),
            "multi_source": done.get("multi_source", 0),
            # Расход прогона календаря — чтобы «сколько стоит» отвечалось
            # журналом, а не расчётом по тарифу (21.08.2026).
            "usage": done.get("usage", {}),
            "artifact_run_id": artifact_run_id,
        },
        ensure_ascii=False,
    )


async def _h_ask_user(args: dict, ctx: ToolContext) -> str:
    """Clarifying question with options. NOTE: in practice this handler is NOT
    executed — `openclaw.stream_events` intercepts an `ask_user` call, emits a
    `clarify` event and ends the turn (the question goes to the user, whose reply
    becomes the next turn). Kept as a safe fallback."""
    return json.dumps(
        {
            "question": str(args.get("question") or "").strip(),
            "options": args.get("options") or [],
            "requires_user_response": True,
        },
        ensure_ascii=False,
    )


# ────────────── finengine extraction tools (RFA port) ──────────────
# These wrap the deterministic finengine extraction engine (ported from
# AIFA_RFA) for the agentic flow. The engine resolves docs via the global
# DocumentStore and does NOT check ownership — so the owner gate MUST live in
# these handlers (mirror api.main._user_can_see). Skipping it would re-open
# the Hotfix-0 IDOR. Compliance: УЭК-АУА-24.


def _fe_resolve_owned_doc(doc_id: str, ctx: ToolContext):
    """Resolve a DocumentStore doc + enforce owner-scoping. Returns
    (doc, index, error_json | None). The engine trusts the caller to have
    checked ownership; this IS that check."""
    from finengine.ingestion.document_store import get_store
    index = get_store()
    if not doc_id:
        return None, index, json.dumps({"error": "doc_id required"}, ensure_ascii=False)
    doc = index.get_document(doc_id)
    if doc is None:
        return None, index, json.dumps({"error": "document not found"}, ensure_ascii=False)
    try:
        if int(getattr(doc, "owner_id", 0)) != int(ctx.user_id):
            return None, index, json.dumps({"error": "access denied"}, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, index, json.dumps({"error": "access denied"}, ensure_ascii=False)
    return doc, index, None


def _fe_doc_text(doc_id: str, limit: int = 200_000) -> str | None:
    """Распознанный текст. Где он лежит — решает `store/blobs`, одно место."""
    from finengine.store.blobs import document_text  # noqa: PLC0415

    return document_text(doc_id, limit)


async def _fe_doc_text_wait(
    doc_id: str, ctx: ToolContext, limit: int = 200_000, timeout_s: float = 240.0
) -> str | None:
    """Like _fe_doc_text, but WAITS for the background OCR to finish when the
    user asks right after upload (race: the .txt sidecar lands a bit after the
    file). Polls up to timeout_s, emitting periodic progress so the stream stays
    alive and the user sees activity instead of a silent bail. Returns the text,
    or None if still absent after the timeout (genuine OCR failure / not a PDF)."""
    # Ожидание — ОДНО на весь код (`store/blobs.document_text_when_ready`):
    # здесь и в `api/internal_docs` оно было написано дважды.
    from finengine.store.blobs import document_text_when_ready  # noqa: PLC0415

    async def _подать_признак_жизни(прошло: float) -> None:
        if ctx.progress is None or int(прошло) % 30:
            return
        try:
            ctx.progress.put_nowait("документ ещё распознаётся (OCR), жду…")
        except Exception:  # noqa: BLE001
            pass

    return await document_text_when_ready(
        doc_id, timeout_s=timeout_s, poll_s=3.0,
        on_wait=_подать_признак_жизни, limit=limit)


async def _h_fe_idp_ocr(args: dict, ctx: ToolContext) -> str:
    """Прогнать документ через IDP-OCR (HTTPS, постранично) и сохранить
    распарсенный текст (.txt-сайдкар). Обычно OCR идёт при загрузке — этот тул
    нужен, если текст ещё не готов или нужен повторный парсинг. Owner-scoped."""
    doc, _index, err = _fe_resolve_owned_doc(str(args.get("doc_id") or "").strip(), ctx)
    if err:
        return err
    from finengine.config import settings
    from finengine.skills.aifa_baseline.idp_ocr_client import call_idp_ocr
    # 🔴 ФАЙЛ БЕРЁМ ИЗ ХРАНИЛИЩА, А НЕ СОБИРАЕМ ПУТЬ. Здесь был
    # `docs_dir / f"{doc_id}_{filename}"` плюс перебор по маске — после переезда
    # документов такого пути на диске нет, и повторный OCR отвечал бы
    # «source PDF not found» на существующий отчёт.
    from contextlib import ExitStack  # noqa: PLC0415

    from finengine.store.blobs import document_idp_sink  # noqa: PLC0415
    from finengine.store.originals import document_original  # noqa: PLC0415

    try:
        if ctx.progress is not None:
            ctx.progress.put_nowait("IDP-OCR: распознавание страниц…")
        with ExitStack() as стек:
            pdf_path = стек.enter_context(document_original(doc))
            raw_path = стек.enter_context(document_idp_sink(doc.doc_id))
            ocr = await call_idp_ocr(pdf_path, save_raw_to=raw_path,
                                     parallel=settings.idp_ocr_parallel)
    except FileNotFoundError:
        return json.dumps({"error": "source PDF not found"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log.exception("fe_idp_ocr failed")
        return json.dumps({"error": f"IDP-OCR failed: {e}"}, ensure_ascii=False)
    full_text = getattr(ocr, "full_text", "") or ""
    # НЕ публиковать пустой сайдкар: иначе статус флипается в «готово», а анализ
    # на пустом тексте — «ещё обрабатывается» (тот же баг, что чинит фоновый путь).
    if full_text.strip():
        try:
            from finengine.store.blobs import save_document_text  # noqa: PLC0415

            # Публикация атомарна внутри хранилища: читатель никогда не увидит
            # наполовину записанный текст.
            save_document_text(doc.doc_id, full_text)
        except Exception:  # noqa: BLE001
            log.exception("fe_idp_ocr sidecar write failed")
    return json.dumps({
        "doc_id": doc.doc_id, "filename": doc.filename,
        "ok": bool(full_text), "chars": len(full_text),
        "endpoint": settings.idp_ocr_url,
    }, ensure_ascii=False)


async def _h_fe_find_report_online(args: dict, ctx: ToolContext) -> str:
    """Найти финансовую отчётность компании в интернете и зарегистрировать
    найденный PDF как ОБЫЧНЫЙ документ — Срез 7 (A5,
    docs/build-plan/17-srez-07-otchetnost-iz-interneta.md).

    Возвращает `doc_id` ровно того же вида, что дала бы ручная загрузка —
    дальше с ним работают `fe_classify_report`/`fe_idp_ocr`/`factor_analysis`
    без каких-либо изменений: отдельного «веб-анализа» нет, документ идёт
    тем же конвейером.

    НЕ `fetch_url`: скачивание идёт через `document_acquisition` — с проверкой
    адреса и redirect'ов (SSRF), лимитом размера, проверкой сигнатуры файла
    (магические байты, не только Content-Type) и дедупом по sha256, а не по
    URL/имени. `fetch_url` для этого не годится и не используется здесь.
    """
    from finengine.skills import web_search as _ws
    from finengine.skills.document_acquisition import find_and_register_report

    if not _ws.is_enabled():
        return json.dumps({
            "error": "Поиск в интернете не настроен. Добавьте FINENGINE_TAVILY_API_KEY в .env.",
        }, ensure_ascii=False)

    company = str(args.get("company") or "").strip()
    if not company:
        return json.dumps({"error": "company required"}, ensure_ascii=False)
    period = str(args.get("period") or "").strip()

    if ctx.user_id is None:
        return json.dumps({"error": "tool requires authorized user context"}, ensure_ascii=False)
    try:
        owner_id = int(ctx.user_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "invalid user context"}, ensure_ascii=False)

    if ctx.progress is not None:
        try:
            ctx.progress.put_nowait(f"Ищу отчётность в интернете: {company} {period}".strip())
        except Exception:  # noqa: BLE001 — прогресс необязателен, не роняем тул из-за него
            pass

    try:
        outcome = await find_and_register_report(company, owner_id=owner_id, period=period)
    except Exception as e:  # noqa: BLE001 — сетевой/поисковый сбой не должен ронять весь ход агента
        log.warning("fe_find_report_online failed for %r: %s", company, e)
        return json.dumps({"error": f"поиск/скачивание не удалось: {e}"}, ensure_ascii=False)

    return json.dumps({
        "status": outcome.status,  # "registered" | "duplicate" | "unavailable"
        "doc_id": outcome.doc_id,
        "source_url": outcome.source_url,
        "source_tier": outcome.source_tier,
        "candidates_tried": outcome.candidates_tried,
        "reason": outcome.reason,
    }, ensure_ascii=False)


async def _h_fe_classify_report(args: dict, ctx: ToolContext) -> str:
    """Классифицировать отчёт: язык / стандарт (IFRS|US GAAP) / тип компании
    (банк|нефинансовая) → ячейка profile_id. Детерминированно, без LLM.

    КЛАССИФИКАЦИЯ — ФАЗА ПРИЁМА ЗАКАЗА (решение владельца 29.08.2026). Понять,
    с чем пришёл пользователь, надо ДО того, как поднята комната станции:
    операция редкая в правках, детерминированная и дешёвая, и держать её у
    модели гейтвея быстрее, чем возить полку в 1,5 МБ ради одного вызова
    `detect_profile`.

    ⚠️ ПАРИТЕТ СО СНЕСЁННЫМ НАВЫКОМ. До этого дня то же самое умел станционный
    `aifa-classify`, и умел БОЛЬШЕ: он объяснял решение (`reason`), различал
    «сказал пользователь» и «догадался классификатор», подсказывал имя
    компании, перечислял доступные роли и — главное — ЧИТАЛ РЕЕСТР ЯЧЕЙКИ,
    превращая сломанный файл в отказ на первом шаге вместо падения через
    минуты работы движка. Снести навык, не перенеся это, значило бы обменять
    скорость на потерю диагностики. Поэтому всё перечисленное здесь ниже.
    """
    doc, _index, err = _fe_resolve_owned_doc(str(args.get("doc_id") or "").strip(), ctx)
    if err:
        return err
    ct = str(args.get("company_type") or "").strip()
    if ct not in ("", "bank", "non_financial"):
        return json.dumps(
            {"error": "company_type: допустимы только 'bank' и 'non_financial'"},
            ensure_ascii=False)
    text = await _fe_doc_text_wait(doc.doc_id, ctx)
    if text is None:
        return json.dumps(
            {"error": "не удалось получить распознанный текст документа "
                      "(OCR не завершился за отведённое время или не дал текста — "
                      "попробуйте ещё раз или перезалейте отчёт)"},
            ensure_ascii=False,
        )
    from finengine.roles.scope import (  # noqa: PLC0415
        EXTRACTION_ROLES, role_supported_for_company_type,
    )
    from finengine.skills.versioning import registry_fingerprint  # noqa: PLC0415
    from finengine.skills.atoms.run_passport import build_run_passport  # noqa: PLC0415

    # Разовая классификация текста — B1 (RunPassport): это ПЕРВОЕ и единственное
    # обращение к `detect_profile` за весь запуск (см. `run_passport.build_run_passport`
    # без `cell`). `aifa-extract`/`aifa-analyze` дальше получают уже решённую
    # `--cell` и текст заново НЕ классифицируют (`classification_from_cell`).
    company_hint = (getattr(doc, "company_name", "") or "").strip()
    try:
        from finengine.config import settings  # noqa: PLC0415
        model_version = settings.openrouter_model
    except Exception:  # noqa: BLE001 — версия модели — диагностика, не блокер
        model_version = ""
    passport = build_run_passport(
        text,
        goal_ir_id=doc.doc_id,
        filename=doc.filename,
        company_hint=company_hint,
        company_type_override=ct or None,
        model_version=model_version,
    )
    classification = passport.classification
    profile_id = classification.profile_id
    company_type = classification.company_type

    # Реестр читаем ЗДЕСЬ, а не только в извлечении: сломанный файл ячейки
    # обязан быть отказом на первом же шаге.
    try:
        версии = registry_fingerprint(profile_id)
    except (FileNotFoundError, ValueError) as exc:
        return json.dumps(
            {"error": f"реестр ячейки '{profile_id}' не читается: {exc}"},
            ensure_ascii=False)

    return json.dumps({
        "doc_id": doc.doc_id,
        "filename": doc.filename,
        # Оба имени: `profile_id` читают старые места, `cell` — тела навыков.
        "cell": profile_id,
        "profile_id": profile_id,
        "standard": classification.standard,
        "company_type": company_type,
        # Основания разные, и агент обязан их различать, когда объясняет выбор.
        "company_type_source": "user" if ct else "classifier",
        "language": classification.language,
        "confidence_low": bool(classification.confidence_low),
        "reason": classification.reason,
        "company_name_hint": company_hint,
        # id.entity (A6): устойчивая идентичность субъекта отчёта, тем же паспортом.
        "entity": {
            "entity_id": passport.entity.entity_id,
            "canonical_name": passport.entity.canonical_name,
            "entity_type": passport.entity.entity_type,
            "confidence_low": passport.entity.confidence_low,
        },
        "registry": {"extractable": версии["extractable"],
                     "calculated": версии["calculated"]},
        "roles_supported": [r for r in EXTRACTION_ROLES
                            if role_supported_for_company_type(r, company_type)],
        "versions": {"registry": версии},
        # RunPassport (Срез 1, B1): паспорт запуска зафиксирован здесь один раз;
        # `--cell` дальше несёт классификацию в `aifa-extract`/`aifa-analyze`,
        # которые пересобирают ЭТОТ ЖЕ паспорт детерминированно, не читая текст
        # заново (см. докстринг `run_passport.build_run_passport`).
        "run_passport": json.loads(passport.model_dump_json()),
    }, ensure_ascii=False)


async def _h_fe_methodology(args: dict, ctx: ToolContext) -> str:
    """#123: правка МЕТОДИКИ анализа — данными, а не дописками в промпт.

    Пользователь поправляет методику словами («работающие активы считай так-то»,
    «горизонт устойчивости не 6 месяцев, а 8»). Раньше единственным способом это
    учесть был лишний абзац в промпте: правка ничем не связана с правилом,
    которое исправляет, две правки одного правила молча противоречат друг другу,
    а обновление базовой методики их не замечает. Здесь правка адресуется
    КОНКРЕТНОМУ элементу и живёт в личном слое пользователя.
    """
    from finengine.skills.analysis import methodology as _md  # noqa: PLC0415
    from finengine.skills.analysis import personal as _personal  # noqa: PLC0415

    uid, _pid = require_user_ctx(ctx)
    uid_i = int(uid)
    action = str(args.get("action") or "list").strip().lower()
    eid = str(args.get("id") or "").strip()
    scope = "turn" if str(args.get("scope") or "always").strip().lower() == "turn" else "always"

    def _err(msg: str, **extra) -> str:
        return json.dumps({"error": msg, **extra}, ensure_ascii=False)

    if action == "list":
        q = str(args.get("query") or "").strip().lower()
        items = _md.catalogue(_personal.effective(uid_i, consume_turn=False))
        if q:
            items = [i for i in items
                     if q in i["title"].lower() or q in i["text"].lower() or q in i["id"].lower()]
        return json.dumps({
            "elements": [{k: i[k] for k in ("id", "title", "kind", "used_in")} for i in items],
            "note": "адресуй правку по id; текст элемента — action=show",
        }, ensure_ascii=False)

    if action == "show":
        if not eid:
            return _err("нужен id элемента (см. action=list)")
        base_el = _md.base().elements.get(eid)
        mine = _personal.effective(uid_i, consume_turn=False).elements.get(eid)
        if base_el is None and mine is None:
            return _err(f"нет элемента «{eid}»")
        return json.dumps({
            "id": eid,
            "title": (mine or base_el).title,
            "base_text": base_el.text if base_el else None,
            "effective_text": mine.text if mine else (base_el.text if base_el else None),
            "changed_by_user": bool(mine and base_el and mine.text != base_el.text),
        }, ensure_ascii=False)

    if action == "mine":
        got = _personal.describe(uid_i)
        return json.dumps({
            **got,
            "note": ("«changes» — что вы поменяли; «notices» — где базовая методика "
                     "с тех пор изменилась и стоит решить, брать ли новое"),
        }, ensure_ascii=False)

    if action == "reset":
        n = _personal.forget(uid_i, eid)
        return json.dumps({
            "removed": n,
            "message": (f"Убрал правок: {n}. Считаем по базовой методике."
                        if n else "Своих правок по этому элементу нет — убирать нечего."),
        }, ensure_ascii=False)

    if action in ("set", "add", "disable"):
        if not eid:
            return _err("нужен id элемента")
        delta: dict = {"op": "replace" if action == "set" else action, "id": eid,
                       "why": str(args.get("why") or "").strip()}
        if action == "set":
            delta["text"] = str(args.get("text") or "").strip()
        elif action == "add":
            delta.update({"text": str(args.get("text") or "").strip(),
                          "title": str(args.get("title") or "").strip() or eid,
                          "group": str(args.get("group") or "").strip(),
                          "after": str(args.get("after") or "").strip()})
        try:
            if scope == "turn":
                _personal.remember_for_turn(uid_i, delta)
            else:
                _personal.save_delta(uid_i, delta)
        except _md.MethodologyError as e:
            # Не применилось — НЕ повод дописать правило в промпт мимо методики.
            # Неприкосновенный элемент (#148) приезжает сюда же: отказ уже несёт
            # причину и подсказку, чем её заменить, — подсказка про id тут лишняя.
            protected = bool((_md.base().elements.get(eid) or _md.Element(eid, eid, "")).protected)
            return _err(str(e), **({} if protected else {"hint": "уточните id через action=list"}))
        base_el = _md.base().elements.get(eid) or _md.Element(eid, eid, "")
        title = base_el.title
        # Выключить раздел, из которого кормится карточка, — законное право
        # пользователя, но последствие обязано быть СКАЗАНО (#148): иначе блок
        # карточки просто исчезнет, и это прочтётся как поломка.
        lost = _md.CARD_ROLE_TITLES.get(base_el.role, "") if action == "disable" else ""
        warn = (f" Учтите: карточка анализа потеряет блок «{lost}» — он собирается "
                f"именно из этого раздела." if lost else "")
        return json.dumps({
            "ok": True, "id": eid, "title": title, "scope": scope,
            **({"card_block_lost": lost} if lost else {}),
            "message": (
                f"Учту в этом анализе: «{title}»." if scope == "turn"
                else f"Записал в вашу методику: «{title}» — так будет считаться всегда. "
                     "Отменить: скажите вернуть базовое правило."
            ) + warn,
        }, ensure_ascii=False)

    return _err(f"неизвестное действие «{action}»",
                actions=["list", "show", "set", "add", "disable", "reset", "mine"])


# ───────────── типизированный исход тула (Ш3, условие приёмки №2) ─────────────
#
# ЗАЧЕМ. «Получилось или нет» до сих пор нигде не выражалось типом. В инлайн-петле
# поле `ok` у события tool_output не эмитилось ВООБЩЕ, а приёмник читал
# `ev.get("ok", True)` — чип в чате был зелёным всегда, включая прогон, вернувший
# ноль показателей. В гейтвей-пути `ok=False` ставился только при ИСКЛЮЧЕНИИ, а
# штатный отказ приезжал обычным json и тоже выглядел успехом.
#
# Спрашивать статус у гейтвея нельзя: доказано, что леджер ядра метит провал как
# `succeeded` (две живые задачи с итогом «Both attempts failed» и статусом
# succeeded). Статус ядра — про то, что процесс завершился, а не про то, что
# работа сделана. Поэтому источник истины ровно один: САМ ТУЛ.
#
# КАК. Конверт навешивается НА ГРАНИЦЕ хендлера, а не правкой каждого return: у
# четырёх тулов их тридцать, и пропустить один — вопрос времени. Все отказы уже
# единообразны (`{"error": …}`), поэтому вывод статуса механический. Тул может
# проставить `status` сам — тогда обёртка его не трогает (так выражается
# `partial`: данные есть, но не все).
OUTCOME_OK = "ok"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
_OUTCOMES = (OUTCOME_OK, OUTCOME_PARTIAL, OUTCOME_FAILED)


def _stamp_outcome(out: str) -> str:
    """Проставить status/reason в json-возврат тула. Не-json — не трогаем."""
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return out
    if not isinstance(data, dict):
        return out
    if data.get("status") in _OUTCOMES:
        return out  # тул высказался сам (например partial) — не переписываем
    err = data.get("error")
    data["status"] = OUTCOME_FAILED if err else OUTCOME_OK
    if err:
        data["reason"] = str(err)
    return json.dumps(data, ensure_ascii=False, default=str)


def _with_outcome(handler):
    """Обернуть хендлер так, чтобы его возврат всегда нёс типизированный исход."""
    import functools  # noqa: PLC0415

    @functools.wraps(handler)
    async def wrapped(args: dict, ctx: ToolContext) -> str:
        return _stamp_outcome(await handler(args, ctx))

    return wrapped


def outcome_of(output: str) -> tuple[str, str] | None:
    """(status, reason) из возврата тула — или None, если конверта нет.

    None означает «тул ещё не переведён на конверт», и это НЕ синоним успеха:
    исполнители в таком случае оставляют прежнее поведение, а не выдумывают
    статус. Список переведённых тулов — `ENVELOPE_TOOLS`.
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status not in _OUTCOMES:
        return None
    return status, str(data.get("reason") or "")


# Тулы, чей возврат обязан нести конверт. Проверяется тестом; расширять по мере
# перевода остальных (сегодня их 17, и у них поведение чипа прежнее).
ENVELOPE_TOOLS = ("fe_idp_ocr",)


# ─────────────────────── registry ──────────────────────


REGISTRY: dict[str, ToolDef] = {
    "fetch_url": ToolDef(
        name="fetch_url",
        description=(
            "Скачать конкретный URL и вернуть его текстовое содержимое "
            "(HTML очищается от тегов). Используй для чтения IR-страниц, "
            "пресс-релизов, документов. Возвращает {url, status, content_type, "
            "text, truncated, full_length}. Если страница JS-only — текст "
            "будет почти пустым, тогда переключайся на tavily_search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный URL с http(s):// префиксом."},
                "max_chars": {
                    "type": "integer",
                    "default": 80000,
                    "minimum": 1000,
                    "maximum": 200000,
                    "description": "Сколько символов текста вернуть (truncate выше).",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=_h_fetch_url,
    ),
    "fe_news_lookup": ToolDef(
        name="fe_news_lookup",
        description=(
            "Поиск по УЖЕ СОБРАННЫМ новостям дайджеста — заголовок, изложение, "
            "ссылка, оценка важности и фраза «почему важно». "
            "ВЫЗЫВАЙ ПЕРВЫМ, когда пользователь спрашивает про новость из "
            "выпуска («что там с…», «зачем…», «подробнее про…»): выпуск уже "
            "собран, и лезть в интернет за тем, что лежит у нас, не нужно. "
            "В интернет (tavily_search) иди ТОЛЬКО если здесь ничего не нашлось."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Одно-два слова из заголовка: «Uber», «Claude акции»."},
                "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 30,
                         "description": "За сколько дней смотреть собранные выпуски."},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_h_news_lookup,
    ),
    "tavily_search": ToolDef(
        # Distinct from OpenClaw's built-in `web_search`. The gateway
        # rejected our previous registration with "client tool name
        # conflict: web_search" — silently, with an empty stream. Naming
        # all our tools with a domain-specific prefix keeps us future-
        # proof against further built-in additions.
        name="tavily_search",
        description=(
            "Поиск свежей информации в интернете через Tavily (наш собственный, "
            "помимо встроенного web_search). Используй для русско/англоязычного "
            "поиска по компаниям из watchlist и регуляторам. Возвращает JSON-массив "
            "{title, url, snippet}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос на любом языке."},
                "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_h_web_search,
    ),
    "list_sources": ToolDef(
        name="list_sources",
        description=(
            "Список файлов-источников загруженных пользователем в текущий "
            "проект (xlsx-watchlist, PDF-отчёт, csv и т.п.). Возвращает "
            "JSON-массив {file_id, filename, content_type, size, "
            "uploaded_at, text_status}. Используй ПЕРВЫМ когда нужно "
            "понять с чем работать, до планирования tavily_search/fetch_url. "
            "Если пользователь говорит 'мой список компаний' / 'этот файл' / "
            "'тот xlsx что я закинул' — начинай отсюда."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_h_list_sources,
    ),
    "read_source": ToolDef(
        name="read_source",
        description=(
            "Прочитать текстовое содержимое source-файла. PDF → текст, "
            "xlsx → TSV-таблица с разделителями по листам, csv/txt/md/json "
            "→ как есть. Использует кэш текста (создан при upload). "
            "Сначала вызови list_sources чтобы получить file_id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "file_id из list_sources"},
                "max_chars": {
                    "type": "integer",
                    "default": 80000,
                    "minimum": 1000,
                    "maximum": 200000,
                    "description": "Лимит длины возвращаемого текста.",
                },
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
        handler=_h_read_source,
    ),
    "fe_idp_ocr": ToolDef(
        name="fe_idp_ocr",
        description=(
            "Прогнать загруженный отчёт через IDP-OCR (постранично, по HTTPS) и "
            "сохранить распознанный текст. Обычно OCR уже сделан при загрузке — "
            "вызывай, если текст документа не готов или нужно повторное распознавание."
        ),
        parameters={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "id документа"},
            },
            "required": ["doc_id"],
            "additionalProperties": False,
        },
        handler=_with_outcome(_h_fe_idp_ocr),
    ),
    "fe_methodology": ToolDef(
        name="fe_methodology",
        description=(
            "Настроить МЕТОДИКУ финансового анализа под пользователя. Вызывай, когда "
            "он поправляет ПРАВИЛО, а не число: «работающие активы считай так-то», "
            "«NIM у нас по средним за квартал», «горизонт устойчивости 8 месяцев», "
            "«не пиши раздел про качественные факторы». Правка цепляется к конкретному "
            "элементу методики и действует ТОЛЬКО для этого пользователя.\n"
            "ОБЯЗАТЕЛЬНО различай горизонт: «в этот раз / сейчас» → scope=turn (учтётся "
            "ближайшим анализом и забудется); «всегда / у нас так принято» → "
            "scope=always. Если из фразы не ясно — СПРОСИ (ask_user) перед записью "
            "always: постоянная правка меняет все будущие отчёты.\n"
            "Действия: list (что вообще можно настроить; сначала посмотри сюда), "
            "show (текст правила), set (заменить правило), add (добавить своё, id "
            "обязан начинаться с «user.»), disable (выключить), reset (вернуть "
            "базовое), mine (что у пользователя своё и где базовая методика с тех пор "
            "изменилась)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["list", "show", "set", "add", "disable", "reset", "mine"]},
                "id": {"type": "string", "description": "id элемента, напр. bank.formula.nim"},
                "text": {"type": "string", "description": "новая формулировка правила"},
                "title": {"type": "string", "description": "имя своего правила (для add)"},
                "group": {"type": "string",
                          "enum": ["analytical.bank", "analytical.non_financial", "comparability"],
                          "description": "куда добавить своё правило (для add)"},
                "after": {"type": "string", "description": "поставить после этого id (для add)"},
                "scope": {"type": "string", "enum": ["always", "turn"],
                          "description": "always — навсегда; turn — только ближайший анализ"},
                "why": {"type": "string", "description": "зачем — сохраняется вместе с правкой"},
                "query": {"type": "string", "description": "поиск по каталогу (для list)"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=_h_fe_methodology,
    ),
    "fe_classify_report": ToolDef(
        name="fe_classify_report",
        description=(
            "Классифицировать загруженный финансовый отчёт: язык, стандарт "
            "(IFRS / US GAAP), тип компании (банк / нефинансовая) → ячейка "
            "profile_id. Детерминированно, без LLM и без подъёма станции — "
            "зови ПЕРЕД работой, чтобы понять, с чем пришёл пользователь. "
            "Отдаёт ещё и объяснение решения, состав реестра ячейки, доступные "
            "роли и подсказку имени компании. Ячейку дальше передают флагом "
            "`--cell`, чтобы второй классификации не было."
        ),
        parameters={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "id документа (из выбранных/проиндексированных)"},
                "company_type": {"type": "string", "enum": ["bank", "non_financial"],
                                 "description": "пользователь НАСТАИВАЕТ на отрасли; "
                                                "пусто — решает классификатор"},
            },
            "required": ["doc_id"],
            "additionalProperties": False,
        },
        handler=_h_fe_classify_report,
    ),
    "fe_find_report_online": ToolDef(
        name="fe_find_report_online",
        description=(
            "Найти финансовую отчётность компании в интернете и зарегистрировать "
            "официальный PDF как обычный документ (Срез 7). Поиск отдаёт приоритет "
            "регулятору/бирже (cbr.ru, e-disclosure.ru, moex.com, sec.gov), проверяет "
            "адрес на SSRF, скачивает бинарный PDF с лимитом размера и проверкой "
            "сигнатуры файла, дедуплицирует по содержимому (sha256), а НЕ по URL/имени. "
            "Возвращает {status: registered|duplicate|unavailable, doc_id, source_url, "
            "source_tier, candidates_tried, reason}. Полученный doc_id используй дальше "
            "в fe_classify_report/fe_idp_ocr/factor_analysis — ровно как doc_id ручной "
            "загрузки, отдельного «веб-анализа» нет."
        ),
        parameters={
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Название компании-эмитента."},
                "period": {"type": "string", "default": "",
                           "description": "Период отчётности, если известен (например '2024' или 'Q1 2025')."},
            },
            "required": ["company"],
            "additionalProperties": False,
        },
        handler=_h_fe_find_report_online,
    ),
    "get_daily_registry": ToolDef(
        name="get_daily_registry",
        description=(
            "Получить заводской реестр IR Daily от заказчика — 79 компаний "
            "(ru / world), 11 макропоказателей (со страной и периодичностью), "
            "7 регуляторов (со страной и фокусом), 13 согласованных СМИ-источников. "
            "Это СТАТИЧНАЯ конфигурация — НЕ для редактирования. Вызывай при "
            "сборке Daily Digest чтобы знать: какие индикаторы публикуются по "
            "какой периодичности, в каком формате сравнения; какие регуляторы "
            "в фокусе для какой страны; на какие источники можно ссылаться."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_h_get_daily_registry,
    ),
    "get_watchlist": ToolDef(
        name="get_watchlist",
        description=(
            "Текущий список компаний, которые пользователь отслеживает в "
            "этом проекте — две группы {ru: [...], world: [...]}. На первый "
            "вызов в новом проекте сидится из get_daily_registry. Дальше "
            "пользователь может менять через update_watchlist. Используй "
            "при сборке Daily чтобы знать кого помечать ⭐ и про кого искать "
            "новости в первую очередь."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_h_get_watchlist,
    ),
    "update_watchlist": ToolDef(
        name="update_watchlist",
        description=(
            "Добавить или убрать компанию из watchlist текущего проекта. "
            "Вызывай когда пользователь сказал «добавь X», «убери Y», «не "
            "следи за Z». Для add нужно указать country (ru/world). Для "
            "remove ищем по имени в обеих корзинах. Имена сравниваются "
            "case-insensitively."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove"]},
                "country": {"type": "string", "enum": ["ru", "world"], "description": "Только для add."},
                "name": {"type": "string", "description": "Название компании как отображать."},
            },
            "required": ["action", "name"],
            "additionalProperties": False,
        },
        handler=_h_update_watchlist,
    ),
    "fe_skill_editor": ToolDef(
        name="fe_skill_editor",
        description=(
            "Редактор навыков: когда пользователь просит ИЗМЕНИТЬ ПОВЕДЕНИЕ/КОД "
            "автоматизации или навыка сверх настроек-ручек (новая колонка, новая "
            "метрика в статистике, другой формат вывода, новая секция) — "
            "предложи правку кода. action=list — какие навыки редактируемы; "
            "action=propose (target, request) — правка+гейт+патч. ВАЖНО отвечать "
            "честно: патч НЕ активируется мгновенно — он сохранён и будет "
            "применён к коду с деплоем (версии/откат через git). Настройки-ручки "
            "и фокус — это fe_scenario_settings, НЕ этот тул."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "propose"]},
                "target": {"type": "string", "description": "Ключ навыка из action=list."},
                "request": {"type": "string", "description": "Что изменить, по-русски, конкретно."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=_h_fe_skill_editor,
    ),
    "fe_scenario_settings": ToolDef(
        name="fe_scenario_settings",
        description=(
            "Настройки автоматизаций (сценариев) пользователя: посмотреть и "
            "изменить расписание/параметры. Вызывай когда пользователь просит "
            "поменять период/окно календаря («смотри 3 недели назад»), "
            "расписание («запускай по будням в 10»), паузу, или спрашивает "
            "текущие настройки автоматизаций. Сначала action=list (взять "
            "scenario_id), затем action=update. Окно календаря — params."
            "back_days/fwd_days В ДНЯХ (недели/месяцы переведи в дни, 1..120). "
            "Расписание меняй ТОЛЬКО по явной просьбе. Поле instructions — "
            "универсальная инструкция сценария: для дайджеста это ФОКУС-ФИЛЬТР "
            "(«только европейский рынок», «только регуляторика») — новости вне "
            "фокуса отбрасываются; для календаря — указания экстрактору событий. "
            "Изменения применяются при следующем запуске."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "update"]},
                "scenario_id": {"type": "string", "description": "Из action=list; только для update."},
                "schedule": {
                    "type": "object",
                    "description": "Новое расписание (только по явной просьбе).",
                    "properties": {
                        "freq": {"type": "string", "enum": ["manual", "hourly", "daily", "weekly"]},
                        "at_time": {"type": "string", "description": "HH:MM МСК"},
                        "dows": {"type": "array", "items": {"type": "integer"}, "description": "Дни недели 0=Пн..6=Вс (для weekly)."},
                    },
                },
                "params": {
                    "type": "object",
                    "description": "Параметры запуска. КАЛЕНДАРЬ: back_days/fwd_days (окно в днях, 1..120), event_groups ({reporting,investor_day,other: bool} — какие типы событий показывать). ДАЙДЖЕСТ: since_hours (часы сбора, 6..336), source_groups ({official,media,telegram,aggregator: bool}), macro_mode (both|week_only|full — какой блок экономики в выпуске). null снимает настройку.",
                },
                "instructions": {"type": "string", "description": "Инструкция сценария (дайджест: фокус-фильтр; календарь: указания экстрактору). Пустая строка очищает."},
                "paused": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=_h_fe_scenario_settings,
    ),
    "factor_analysis": ToolDef(
        name="factor_analysis",
        description=(
            "Провести факторный анализ отклонения Факт-План по производственно-"
            "сбытовым показателям на основе xlsx-файлов проекта (план, факт, "
            "опц. справочник номенклатуры). Возвращает: agregaты выручки/"
            "себестоимости/маржи, декомпозицию на факторы (объём, структура, "
            "цена, материалы, ФОТ, накладные хоз.службы/УК, качество, гарантия), "
            "разбивку по продуктам, методологические заметки. \n\n"
            "Используй когда пользователь просит 'провести факторный анализ', "
            "'разложить отклонение от плана', 'почему маржа упала' — после того "
            "как загрузил план/факт xlsx. \n\n"
            "ВАЖНО: после получения результата отдай содержательный комментарий "
            "по top-3 факторам, не перечисляй сырой JSON. Цифры в ответе цитируй "
            "именно из поля factors[]/aggregates — они корректно посчитаны Python, "
            "не пересчитывай в уме."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan_file_id": {
                    "type": "string",
                    "description": "file_id плана из list_sources. Если не указан — "
                                   "автоопределение по имени файла (план/plan).",
                },
                "fact_file_id": {
                    "type": "string",
                    "description": "file_id факта из list_sources. Если не указан — "
                                   "автоопределение по имени файла (факт/fact).",
                },
                "nomenclature_file_id": {
                    "type": "string",
                    "description": "Optional: file_id справочника номенклатуры.",
                },
                "preset": {
                    "type": "string",
                    "enum": ["default", "mvp", "revenue_only"],
                    "description": "Набор факторов. default=все 9 (полный анализ "
                                   "по ТЗ ТМХ), mvp=4 ключевых (выручка+материалы), "
                                   "revenue_only=Q×S×P. По умолчанию НЕ передавай "
                                   "этот параметр — будет default (9 факторов).",
                },
                "products": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Коды продуктов для фильтра (None=все).",
                },
                "factors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Явный список факторов (переопределяет preset). "
                                   "Доступны: volume, structure, price, materials, "
                                   "labor, overhead_plant, overhead_mgmt, quality, warranty.",
                },
                "period_label": {
                    "type": "string",
                    "description": "Подпись периода для отчёта, напр. 'Q1 2026'.",
                },
            },
            "additionalProperties": False,
        },
        handler=_h_factor_analysis,
    ),
    "daily_news_digest": ToolDef(
        name="daily_news_digest",
        description=(
            "Собрать IR Daily Digest через backend ETL-pipeline. RSS/API "
            "коннекторы, нормализация, дедуп, rule-фильтр, один LLM batch для "
            "классификации, готовый markdown на выходе. Возвращает {markdown, "
            "items_raw, items_kept, cost_usd, duration_s, from_cache, run_id}. "
            "Вызывай ТОЛЬКО на ЯВНУЮ просьбу новостного дайджеста: «собери "
            "дайджест», «daily digest», «новости за день». НЕ вызывай на "
            "непонятные/опечаточные фразы и на вопросы про показатели отчёта. "
            "Один вызов = готовый ответ — не делай tavily_search/fetch_url "
            "дополнительно. Свежий прогон идёт 5-7 МИНУТ (сбор со ~180 "
            "источников плюс классификация), из кэша — мгновенно. Дождись "
            "ответа: не перезапускай и не зови другие инструменты в это время."
        ),
        parameters={
            "type": "object",
            "properties": {
                "fresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Принудительно перезапустить pipeline даже если есть свежий кэш. Используй когда юзер просит «пересобери» или «свежий».",
                },
                # 🔴 БЕЗ `default` (24.08.2026). Схема объявляла модели
                # «default: 48», и она послушно передавала 48 КАЖДЫЙ РАЗ — а
                # явно переданное значение сильнее сохранённых настроек
                # владельца. Итог: обработчик читал сценарий верно, но до него
                # доезжало чужое число. Живой прогон админа 24.08 в 08:46 снова
                # собрал 3902 новости вместо 671, хотя правка была выкачена:
                # чинил обработчик, а врала СХЕМА.
                #
                # `top_n` из видимых модели снят целиком: это ручка стоимости и
                # полноты (потолок кандидатов, настройка news_top_candidates),
                # решать её модели не за что, а её «maximum: 60» вдобавок
                # зажимал настроенные 100.
                "since_hours": {
                    "type": "integer",
                    "minimum": 6,
                    "maximum": 336,
                    "description": (
                        "Окно сбора в часах. НЕ ПЕРЕДАВАЙ это поле, если "
                        "пользователь не назвал период словами («за сутки», "
                        "«за три дня»): без него берутся настройки владельца со "
                        "страницы автоматизаций, и чат собирает ровно то же, "
                        "что кнопка «Запустить»."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_h_daily_news_digest,
    ),
    "ir_calendar_run": ToolDef(
        name="ir_calendar_run",
        description=(
            "Собрать календарь корпоративных событий (отчётности, дни инвестора, "
            "ОСА, дивиденды) на окно ±N дней (умолчание 14, задаётся "
            "back_days/fwd_days) — ПРЕДСТОЯЩИЕ + НЕДАВНИЕ по факту — из "
            "нескольких источников (IR-сайты, Finnhub, e-disclosure, smart-lab) с "
            "пометкой уверенности (multi_source). Возвращает {companies_scanned, "
            "companies_failed, failed_companies, companies_with_events, events_count, "
            "by_source, multi_source, events[], artifact_run_id}.\n"
            "РЕЖИМЫ:\n"
            "• «запусти/собери календарь», «весь календарь» → вызывай БЕЗ параметров → "
            "ПОЛНЫЙ скан ВСЕХ компаний покрытия. Идёт несколько минут — это нормально, "
            "дождись результата (агент в фоновой задаче, переживает обрыв связи).\n"
            "• «когда отчитывается X / события по X» → companies=[...] с названиями — "
            "точечно и быстро.\n"
            "Один вызов = ответ; 🚫 не делай fetch_url/tavily_search. ОБЯЗАТЕЛЬНО начни "
            "ответ со СТАТУСА: «просканировано N, с событиями M, не удалось K "
            "(failed_companies — сайт не открылся), у остальных нет событий в окне» — "
            "чтобы было понятно, пусто это «нет событий» или «не удалось прочитать». Затем "
            "markdown-таблица со ВСЕМИ событиями из поля `events` (все строки — НЕ "
            "сокращай, НЕ пиши «уточните запрос для деталей»): дата/тип/компания/ссылка, "
            "✓✓ где multi_source≥2."
        ),
        parameters={
            "type": "object",
            "properties": {
                "companies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Названия компаний из покрытия (напр. ['Apple','Netflix','Magnit']) — точечный быстрый скан только их. Пусто → полный скан всех (если `limit` тоже пуст).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 72,
                    "description": "НЕ задавай для «запусти календарь» — без него полный скан ВСЕХ компаний. Число — только если юзер явно просит маленький срез (первые N).",
                },
                # 🔴 ЭТИХ ПОЛЕЙ НЕ БЫЛО ВОВСЕ (25.08.2026). Просьба «собери
                # календарь за 1 день» не срабатывала: выразить окно модели
                # было НЕЧЕМ, и скан всегда шёл ±14 — в логе прода это видно по
                # запросам к Finnhub `from=2026-08-11&to=2026-09-08`. Движок
                # окно принимал, в сценарии ручка работала; не доезжало ровно
                # до чата.
                # Без `default`, по вчерашнему уроку с дайджестом: умолчание в
                # схеме модель читает как «передавай это значение», и оно
                # перебивает сохранённые настройки владельца.
                "back_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": (
                        "На сколько дней смотреть НАЗАД (уже опубликованные "
                        "события). Задавай, когда период назван словами: «за 1 "
                        "день» → 1, «за неделю» → 7, «за месяц» → 30. Без этого "
                        "поля берутся настройки владельца, иначе 14."
                    ),
                },
                "fwd_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": (
                        "На сколько дней смотреть ВПЕРЁД (объявленные события). "
                        "Обычно равно back_days: «за 1 день» → 1. Без этого "
                        "поля берутся настройки владельца, иначе 14."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_h_ir_calendar_run,
    ),
    "ask_user": ToolDef(
        name="ask_user",
        description=(
            "Задать пользователю ОДИН короткий уточняющий вопрос с 2–5 вариантами "
            "ответа — КОГДА ты действительно не можешь продолжить уверенно: трактовка "
            "задачи или объём данных неоднозначны и неверный дефолт приведёт к "
            "бесполезной/неверной работе; либо перед дорогой (полный ir_calendar_run — "
            "минуты) или необратимой операцией. НЕ используй для мелочей и когда есть "
            "разумный дефолт — действуй сам. НИКОГДА не спрашивай «какой документ/файл», "
            "если документ уже приложен, и не спрашивай период/метрику/вид анализа, если "
            "задачу можно выполнить с дефолтом (свежий период, полное покрытие, general). "
            "Возвращает управление пользователю: его "
            "выбор придёт следующим сообщением."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Короткий вопрос по существу (напр. «За какой период считать?»).",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 5,
                    "description": "2–5 коротких вариантов ответа для выбора.",
                },
            },
            "required": ["question", "options"],
            "additionalProperties": False,
        },
        handler=_h_ask_user,
    ),
    # ir_daily_run removed 2026-05-21 (Daily News → daily_news_digest); its
    # handler was deleted. ir_calendar_run re-enabled above (hybrid multi-source).
}


# ─────────────────── whitelist gate ──────────────────


def _load_whitelist() -> set[str] | None:
    """Read enabled[] from deploy/tools_whitelist.yaml. Returns set of
    enabled tool names, or None to mean "all registered" (fail-open if
    config is missing or malformed — log a warning).

    Per the AUA regulator (УЭК-АУА-7, УЭК-АУА-13): only tools from a
    controlled catalog. This YAML is that catalog — git-tracked,
    reviewable, and gates what's exposed to the agent.
    """
    import os
    from pathlib import Path

    # Look up the deploy/ dir relative to this file (… /src/finengine/llm)
    # then back up to repo root. In docker the layout is /app/src/finengine
    # plus /app/deploy/. Both work with parents[3].
    repo_root = Path(__file__).resolve().parents[3]
    yaml_path = Path(
        os.environ.get("FINENGINE_TOOLS_WHITELIST_YAML")
        or repo_root / "deploy" / "tools_whitelist.yaml"
    )
    if not yaml_path.exists():
        log.warning(
            "tools whitelist not found at %s — falling back to ALL registered "
            "tools. Set FINENGINE_TOOLS_WHITELIST_YAML or place the file in deploy/.",
            yaml_path,
        )
        return None
    try:
        # Tiny dependency-free YAML subset parser — we only need
        # `enabled:` list. Avoids pulling in PyYAML for one file.
        text = yaml_path.read_text(encoding="utf-8")
        enabled: set[str] = set()
        in_enabled = False
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            if line.startswith("enabled:"):
                in_enabled = True
                continue
            if in_enabled:
                stripped = line.lstrip()
                if line[:1] in (" ", "\t") and stripped.startswith("- "):
                    # "  - name  # comment"
                    name = stripped[2:].split("#", 1)[0].strip()
                    if name:
                        enabled.add(name)
                elif not line.startswith((" ", "\t")):
                    # Top-level key after enabled[] ended the list.
                    in_enabled = False
        log.info("tools whitelist loaded from %s: %s", yaml_path, sorted(enabled))
        return enabled
    except Exception as e:  # noqa: BLE001
        log.warning("failed to parse tools whitelist %s: %s — using all registered", yaml_path, e)
        return None


_WHITELIST = _load_whitelist()


def all_definitions() -> list[dict]:
    """Serialise tools for the agent — only those allowed by the
    whitelist YAML. If the YAML is missing/malformed we fall back to
    every registered tool (logged at startup)."""
    if _WHITELIST is None:
        return [t.to_openclaw() for t in REGISTRY.values()]
    return [t.to_openclaw() for name, t in REGISTRY.items() if name in _WHITELIST]


async def execute(name: str, args: dict, ctx: ToolContext) -> str:
    """Dispatch a function_call to its handler. Refuses tools not in
    the whitelist even if the agent somehow knew about them (defense
    against tool-name spoofing in cached agent state)."""
    import time as _time
    from finengine.policy.killswitch import is_killed
    from finengine import audit as _audit

    started = _time.perf_counter()
    error: str | None = None
    result: str

    killed, reason = is_killed()
    if killed:
        result = json.dumps(
            {"error": f"agent disabled by ops: {reason}"},
            ensure_ascii=False,
        )
        error = "kill_switch"
    elif _WHITELIST is not None and name not in _WHITELIST:
        log.warning("blocked tool call %r — not in whitelist", name)
        result = json.dumps(
            {"error": f"tool {name} is not enabled in this deployment"},
            ensure_ascii=False,
        )
        error = "not_whitelisted"
    else:
        tool = REGISTRY.get(name)
        if not tool:
            result = json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
            error = "unknown_tool"
        else:
            try:
                result = await tool.handler(args, ctx)
            except Exception as e:  # noqa: BLE001
                log.exception("tool %s crashed", name)
                result = json.dumps({"error": f"tool crashed: {e}"}, ensure_ascii=False)
                error = f"{e.__class__.__name__}: {e}"

    _audit.log_tool_call(
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        session_id=ctx.session_id,
        tool_name=name,
        args=args,
        result_preview=result,
        latency_ms=(_time.perf_counter() - started) * 1000.0,
        error=error,
    )
    return result
