"""#98 (C3-2): внутренняя ручка исполнения fe-тулов для плагина гейтвея.

Целевая ось: петлю агента ведёт ГЕЙТВЕЙ, его плагин fe-tools проксирует вызовы
инструментов сюда. Owner-скоуп — СЕРВЕРНЫЙ: user/project берутся из маппинга
gateway-session → контекст хода (регистрирует раннер /chat/turns при старте
хода), модели и аргументам тула здесь не доверяем.

Протокол — ТИКЕТ + ПОЛЛИНГ (долгие тулы: extract/календарь идут минуты, а
долгий HTTP-запрос убивает fetch-таймаут в цепочке плагин→бэк; эмпирика:
ir_calendar_run умер молча, tool_output так и не родился):
  mode="start" → спавним asyncio-задачу, мгновенно отвечаем {ticket};
  mode="poll"  → 202 running | 200 конверт результата | 409 ход отменён
                 (задача при этом отменяется — «Стоп» гасит и in-flight тул).
Без mode — синхронное исполнение (fe_ping, тесты).

Auth: тот же внутренний секрет (gateway-токен), что у ingest/doc-text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter()

# gateway_session_id → контекст хода. Регистрирует раннер /chat/turns.
#
# 🔴 ЖИВЁТ В ОБЩЕМ СОСТОЯНИИ, А НЕ В ПАМЯТИ ПРОЦЕССА (правило 3.2). Ход
# регистрируется раннером на ОДНОЙ реплике, а вызов тула от гейтвея приходит
# на ЛЮБУЮ. Пока словарь был локальным, второй воркер отвечал 403 «ход не
# зарегистрирован» на КАЖДЫЙ вызов — ход оставался без инструментов вовсе.
#
# Здесь нет своего хранилища: это тот же `store/state`, что у сессий и списков
# наблюдения. Правки читают-меняют-пишут через `mutate` — иначе два
# одновременных вызова тула теряют один из своих следов.
_TTL_SEC = 3600.0

# ticket → {task, run_id, exp}. ЗАДАЧА остаётся у реплики-владельца: перенести
# `asyncio.Task` между процессами нельзя. Общее — только ИСХОД: владелец кладёт
# его в состояние, а поллинг читает откуда угодно.
_tickets: dict[str, dict] = {}
_TICKET_TTL_SEC = 1800.0


def _ctx_get(gateway_session: str) -> dict | None:
    """Контекст хода. `None` — не зарегистрирован или истёк."""
    from finengine.store import state  # noqa: PLC0415

    if not gateway_session:
        return None
    ctx = state.get(state.GATEWAY_SESSION, owner="_", key=gateway_session)
    if ctx is None:
        return None
    # ⚠️ СРОК ХРАНИМ ВНУТРИ ЗАПИСИ, А НЕ ЧАСАМИ ПРОЦЕССА. Прежний код держал
    # `exp` по `time.monotonic()` — счётчику ОДНОГО процесса, у соседней
    # реплики он свой. Запись, записанная там, здесь читалась бы как вечная
    # или как всегда просроченная.
    if float(ctx.get("exp") or 0) < time.time():
        return None
    return ctx


def _ctx_mutate(gateway_session: str, изменить) -> dict | None:
    """Прочитать-изменить-записать контекст хода одной операцией."""
    from finengine.store import state  # noqa: PLC0415

    if not gateway_session:
        return None
    return state.mutate(state.GATEWAY_SESSION, owner="_", key=gateway_session,
                        изменить=изменить)


def reset_sessions() -> None:
    """Забыть все контексты ходов. Только для тестов.

    Раньше это был `_session_ctx.clear()` — словарь жил в памяти, и
    уборка была бесплатной. Теперь состояние общее, и забывать его надо
    явно: иначе ход одного теста виден следующему, и отметка вызванных
    тулов копится через весь набор."""
    from finengine.store import state  # noqa: PLC0415

    for ключ in state.keys(state.GATEWAY_SESSION, owner="_"):
        state.delete_one(state.GATEWAY_SESSION, owner="_", key=ключ)
    _tickets.clear()


def register_session(gateway_session: str, *, user_id: int, project_id: str,
                     run_id: str, session_id: str = "") -> None:
    """Маппинг ключ хода → владелец. Зовётся ДВАЖДЫ за ход: на старте и когда
    агент сообщит `session_id`.

    ⚠️ СЛИЯНИЕ, А НЕ ЗАМЕНА (#190/К8b). Запись пересобиралась целиком, и второй
    вызов стирал всё, чего нет в его аргументах, — в частности признак
    дословности (`verbatim_text`). Пока его писал только тул, порядок спасал:
    тулы исполняются позже обеих регистраций. Слушатель прогона станции пишет
    туда же и МИНУТАМИ позже, то есть в произвольном порядке относительно
    регистраций, — и тогда «канон отдан» молча исчезало бы, а модель дописывала
    поверх готовой таблицы свой пересказ.
    """
    def _слить(было: dict | None) -> dict:
        ctx = dict(было or {})
        ctx.update({
            "user_id": user_id, "project_id": project_id, "run_id": run_id,
            "session_id": session_id, "exp": time.time() + _TTL_SEC,
        })
        return ctx

    _ctx_mutate(gateway_session, _слить)


def _append_verbatim(gateway_session: str, text: str) -> None:
    """ЕДИНСТВЕННЫЙ писатель признака дословности.

    Глушилка пересказа читает `verbatim_text_of`, и завести для станционного
    пути второе хранилище значило бы расстроить её для `fe_analyze` и
    дайджеста: они получили бы канон, а следом пересказ поверх.

    ⚠️ ЧЕРЕЗ `mutate`, А НЕ ЧЕРЕЗ СЛОВАРЬ НА РУКАХ. Два тула одного хода
    исполняются параллельно, и «прочитать свой снимок → дописать → записать»
    теряет чужую дописку БЕЗ ошибки: канон уже на экране, а глушилка о нём не
    знает и пропускает пересказ поверх.
    """
    def _дописать(было: dict | None) -> dict:
        ctx = dict(было or {})
        ctx["verbatim_text"] = (ctx.get("verbatim_text") or "") + text
        return ctx

    _ctx_mutate(gateway_session, _дописать)


def mark_verbatim(gateway_session: str, text: str) -> None:
    """То же ПО КЛЮЧУ ХОДА — для тех, у кого на руках нет словаря контекста.

    Так пишет слушатель прогона станции: он живёт вне цикла исполнения тула и
    знает только ключ.

    Пустой ключ — не сбой: ход мог идти без гейтвей-петли, и глушить нечего.
    Незнакомый ключ — сбой, и молчать о нём нельзя: канон уже на экране, а
    пересказ поверх него никто не остановит.
    """
    if not gateway_session or not text:
        return
    if _ctx_get(gateway_session) is None:
        log.warning("verbatim: ключ хода …%s не зарегистрирован — глушилка "
                    "пересказа не сработает", gateway_session[-8:])
        return
    _append_verbatim(gateway_session, text)


def _token() -> str:
    # Единственная реализация правила — `api/internal_auth`.
    from finengine.api.internal_auth import gateway_token  # noqa: PLC0415

    return gateway_token()


def verbatim_text_of(gateway_session: str) -> str:
    """Канонический markdown, уже показанный пользователю в этом ходе
    (verbatim-мост). Раннер по нему глушит пересказ модели и пишет аудит-лог."""
    return (_ctx_get(gateway_session) or {}).get("verbatim_text") or ""


def tools_called_of(gateway_session: str) -> list[str]:
    """Имена тулов, исполненных бэкендом за ЭТОТ ход, по порядку вызова.

    #235: в гейтвей-петле тулы зовёт гейтвей, и раннер их не видит — отметка
    хода печатала `tools=0` всегда, то есть отвечала на вопрос «сколько
    вызовов ОСТАЛОСЬ», притворяясь ответом на «сколько их БЫЛО». Разбирать
    прогон приходилось по трассе ядра на VPS. Ключ хода — свежий uuid на
    каждый ход, поэтому накопление по ключу и есть накопление по ходу.
    """
    return list((_ctx_get(gateway_session) or {}).get("tools_called") or [])


# Тяжёлые поля результата, которые модель НЕ должна пересказывать (канон уже
# отдан пользователю дословно) — вырезаются из ответа плагину.
_VERBATIM_STRIP = ("markdown", "table_markdown", "report_markdown", "report_structured")


class _ProgressBridge:
    """ctx.progress для тулов гейтвей-петли: интерфейс asyncio.Queue.put_nowait,
    но элементы конвертируются штатным конвертером (_progress_events из
    openclaw.py) и эмитятся прямо в chat-ран хода."""

    def __init__(self, run_id: str, tool_name: str):
        self._run_id = run_id
        self._tool = tool_name

    def put_nowait(self, item) -> None:  # noqa: ANN001
        if not self._run_id:
            return
        try:
            from finengine.llm.openclaw import _progress_events  # noqa: PLC0415
            from finengine.skills import run_orchestrator as ro  # noqa: PLC0415
            for ev in _progress_events(item, self._tool):
                ro.emit_to_run(self._run_id, ev)
        except Exception:  # noqa: BLE001 — прогресс не должен ронять тул
            log.debug("progress bridge drop", exc_info=True)


async def _execute_tool(tool: str, args: dict, ctx: dict,
                        gateway_session: str = "") -> dict:
    """Исполнить тул с чипами/историей/verbatim-мостом. Возвращает конверт
    {"ok": ...} для плагина (модели). Вся пост-обработка ЗДЕСЬ, чтобы жить в
    фоновой задаче независимо от HTTP-запросов поллинга."""
    from finengine.llm import tools as _tools  # noqa: PLC0415
    from finengine.skills import run_orchestrator as _ro  # noqa: PLC0415

    run_id = ctx.get("run_id") or ""
    tool_def = _tools.REGISTRY[tool]

    class _Ctx:
        user_id = str(ctx["user_id"])
        project_id = ctx["project_id"]
        # Прогресс-мост: put_nowait(str|dict) → те же SSE-события, что в
        # инлайн-петле (tool_progress/extract_start/metric_row/…) → chat-ран.
        progress = _ProgressBridge(run_id, tool)

    # Чипы тулов + история сессии — в гейтвей-петле их эмитит БЭК в момент
    # исполнения (стрим гейтвея отдаёт только финальный текст, function_call
    # итемов в нём нет — проверено эмпирически). Session-объект тот же
    # in-process экземпляр, что держит агент хода, — дописываем хронологично.
    session = None
    try:
        from finengine.api.main import get_agent  # noqa: PLC0415
        from finengine.orchestrator.agent import _summarize_tool_args  # noqa: PLC0415
        if ctx.get("session_id"):
            session = get_agent().get_or_create_session(
                ctx["session_id"], auth_user_id=int(ctx["user_id"]))
        _ro.emit_to_run(run_id, {
            "type": "tool_call", "name": tool,
            "args_summary": _summarize_tool_args(tool, args),
            # 🔴 ПОМЕТКА ГОНКИ (31.08.2026). Это событие идёт с ПРОКСИ-вызова —
            # другим каналом, чем текст модели, и приходит фронту ПОСРЕДИ её
            # предложения. Фронт по обычному tool_call перекладывает написанное
            # в ленту хода — здесь так нельзя: резало «…затем посчитаю |
            # коэффициенты.» пополам, а финальный ответ уезжал в заметки.
            # С пометкой фронт ставит шаг в ленту, но текст не трогает.
            "proxied": True,
        })
        if session is not None:
            session.add_tool_call(tool, args)
    except Exception:  # noqa: BLE001 — чипы/история не должны ронять тул
        log.debug("tool chip/history (pre) failed", exc_info=True)

    result = None
    reason = ""
    try:
        result = await tool_def.handler(args, _Ctx())
        # Ш3: до этого ok=True ставился ВСЕГДА, когда не было исключения, —
        # штатный отказ («ноль показателей», «документ не найден») приезжал
        # обычным json и выглядел успехом. Теперь исход берётся из конверта
        # тула. У тулов без конверта поведение прежнее: выдумывать за них
        # статус — та же ложь, только с другой стороны.
        outcome = _tools.outcome_of(result if isinstance(result, str) else "")
        if outcome is not None:
            status, reason = outcome
            ok = status != _tools.OUTCOME_FAILED
        else:
            ok = True
        out = {"ok": ok, "result": result}
    except asyncio.CancelledError:
        # «Стоп»: задачу отменил поллер (ход завершён). Чип честно закрываем.
        try:
            _ro.emit_to_run(run_id, {"type": "tool_output", "name": tool, "ok": False})
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("internal tool call failed: %s", tool)
        ok, out = False, {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Verbatim-мост: pre-rendered тулы (дайджест/календарь/extract/analyze)
    # отдают канонический markdown ПОЛЬЗОВАТЕЛЮ дословно (side-события + текст
    # прямо в ран), а МОДЕЛИ — усечённый результат с запретом пересказа. Тот же
    # контракт, что _verbatim_render в инлайн-петле. ⚠️ Хендлеры возвращают
    # JSON-СТРОКИ (json.dumps), не dict — _verbatim_render парсит строку сам
    # (грабля: isinstance(result, dict)-гвард молча выключал мост целиком).
    if ok:
        try:
            from finengine.orchestrator.agent import (  # noqa: PLC0415
                _VERBATIM_TOOLS, _verbatim_render)
            if tool in _VERBATIM_TOOLS:
                output_full = (result if isinstance(result, str)
                               else json.dumps(result, ensure_ascii=False, default=str))
                vtext, vevents = _verbatim_render(tool, output_full)
                for ev in vevents:
                    _ro.emit_to_run(run_id, ev)
                if vtext:
                    _ro.emit_to_run(run_id, {"type": "text_delta", "text": vtext})
                    _append_verbatim(gateway_session, vtext)
                if vtext or vevents:
                    try:
                        parsed = json.loads(output_full)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    slim = ({k: v for k, v in parsed.items() if k not in _VERBATIM_STRIP}
                            if isinstance(parsed, dict) else {})
                    slim["_verbatim_note"] = (
                        "Полный отчёт уже показан пользователю дословно. НЕ "
                        "пересказывай и не сокращай его — подтверди кратко (1–2 "
                        "фразы) и предложи следующий шаг.")
                    out = {"ok": True, "result": slim}
        except Exception:  # noqa: BLE001 — verbatim не должен ронять тул
            log.exception("verbatim bridge failed: %s", tool)

    try:
        _chip: dict = {"type": "tool_output", "name": tool, "ok": ok}
        if reason:
            # Причина отказа — на экран. Без неё пользователь видит красный чип
            # и не знает, что случилось; это тот же молчаливый отказ, только
            # покрашенный.
            _chip["reason"] = reason[:400]
        _ro.emit_to_run(run_id, _chip)
        if session is not None:
            # В историю — ПОЛНЫЙ результат (не verbatim-slim): follow-up'ам
            # нужны factors[]/aggregates/периоды, как в инлайн-петле.
            output_full = json.dumps(
                result if ok else out, ensure_ascii=False, default=str)
            session.add_tool_output(tool, ok, output_full)
    except Exception:  # noqa: BLE001
        log.debug("tool chip/history (post) failed", exc_info=True)
    return out


def _purge_tickets() -> None:
    now = time.monotonic()
    for k in [k for k, v in _tickets.items() if v.get("exp", 0) < now]:
        t = _tickets.pop(k, None)
        if t and t.get("task") is not None and not t["task"].done():
            t["task"].cancel()


# ── Тикет: локальная задача + общий исход ────────────────────────────────
# 🔴 РАЗДЕЛЕНИЕ ТО ЖЕ, ЧТО У ПРОГОНА. `asyncio.Task` между процессами не
# переносится, поэтому у владельца остаётся задача, а в общее состояние
# кладётся ИСХОД. Поллинг гейтвея приходит на любую реплику и читает исход
# оттуда — раньше он получал 404 «unknown ticket» и результат долгого тула
# (извлечение идёт минутами) терялся целиком.
_ТИКЕТЫ_КАНАЛ = "tickets:control"
_слушатель_тикетов: "asyncio.Task | None" = None


def _ключ_тикета(ticket: str) -> str:
    return f"ticket:{ticket}"


def _тикет(ticket: str) -> dict | None:
    from finengine.store.cache import get_cache  # noqa: PLC0415

    try:
        return get_cache().get(_ключ_тикета(ticket))
    except Exception:  # noqa: BLE001
        log.warning("тикет %s не прочитан", ticket[:8], exc_info=True)
        return None


def _записать_тикет(ticket: str, **поля) -> None:
    from finengine.store.cache import get_cache  # noqa: PLC0415

    try:
        кэш = get_cache()
        запись = кэш.get(_ключ_тикета(ticket)) or {}
        запись.update(поля)
        кэш.set(_ключ_тикета(ticket), запись, _TICKET_TTL_SEC)
    except Exception:  # noqa: BLE001 — координация не роняет тул
        log.warning("тикет %s не записан", ticket[:8], exc_info=True)


async def _исполнить_под_тикет(ticket: str, tool: str, args: dict, ctx: dict,
                               gateway_session: str) -> None:
    """Исполнить тул и положить исход туда, откуда его прочтёт любая реплика.

    Отказ кладём тем же путём, что успех: поллинг обязан получить ответ, а не
    ждать вечно на задаче, которой уже нет.
    """
    try:
        итог = await _execute_tool(tool, args, ctx, gateway_session)
    except asyncio.CancelledError:
        _записать_тикет(ticket, done=True, cancelled=True)
        raise
    except Exception as e:  # noqa: BLE001 — исход отказа тоже исход
        _записать_тикет(ticket, done=True,
                        out={"ok": False, "error": f"{type(e).__name__}: {e}"})
        return
    _записать_тикет(ticket, done=True, out=итог)


def _ход_здесь(run_id: str) -> bool:
    """Идёт ли прогон этого хода на ЭТОЙ реплике."""
    from finengine.skills import run_orchestrator as _ro  # noqa: PLC0415

    return bool(run_id) and _ro.get_run(run_id) is not None


async def _слушать_тикеты() -> None:
    """Один слушатель на процесс: гасит свои тикеты и берёт свою работу.

    Канал один на все тикеты, а не по каналу на тикет: подписка стоит
    соединения, а тикетов за ход бывает десяток. Сообщение получают все
    реплики; действует та, у которой прогон хода.
    """
    from finengine.store.bus import get_bus  # noqa: PLC0415

    подписка = get_bus().subscribe(_ТИКЕТЫ_КАНАЛ)
    try:
        while True:
            сообщение = await подписка.get(timeout=5.0)
            if сообщение is None:
                continue
            приказ = сообщение.get("cmd")
            if приказ == "cancel":
                свой = _tickets.pop(сообщение.get("ticket") or "", None)
                if свой and свой.get("task") is not None and not свой["task"].done():
                    свой["task"].cancel()
                continue
            if приказ == "run_tool":
                ctx = сообщение.get("ctx") or {}
                if not _ход_здесь(ctx.get("run_id") or ""):
                    continue  # не наш ход — работу возьмёт владелец
                _начать_локально(сообщение.get("ticket") or "",
                                 сообщение.get("tool") or "",
                                 сообщение.get("args") or {}, ctx,
                                 сообщение.get("gateway_session") or "")
    except asyncio.CancelledError:
        raise
    finally:
        подписка.close()


def _начать_локально(ticket: str, tool: str, args: dict, ctx: dict,
                     gateway_session: str) -> None:
    """Запустить тул здесь и запомнить задачу — чтобы было что гасить."""
    _tickets[ticket] = {
        "task": asyncio.create_task(
            _исполнить_под_тикет(ticket, tool, args, ctx, gateway_session)),
        "run_id": ctx.get("run_id") or "",
        "exp": time.monotonic() + _TICKET_TTL_SEC,
    }


def запустить_слушателя() -> None:
    """Поднять слушателя тикетов. Зовётся на старте КАЖДОЙ реплики.

    Лениво (при первом `mode=start`) нельзя: реплика, которая сама тулов не
    начинала, всё равно обязана слышать работу для СВОЕГО хода. Без этого
    переадресация молчала бы, а тикет висел бы «running» до истечения срока.
    """
    global _слушатель_тикетов
    if _слушатель_тикетов is not None and not _слушатель_тикетов.done():
        return
    try:
        _слушатель_тикетов = asyncio.get_running_loop().create_task(_слушать_тикеты())
    except RuntimeError:
        log.debug("слушатель тикетов не поднят: нет цикла событий")


def _погасить_тикет(ticket: str) -> None:
    """Отменить тикет здесь, а если он чужой — попросить владельца."""
    свой = _tickets.pop(ticket, None)
    if свой is not None:
        if свой.get("task") is not None and not свой["task"].done():
            свой["task"].cancel()
        return
    from finengine.store.bus import get_bus  # noqa: PLC0415

    try:
        asyncio.get_running_loop().create_task(
            get_bus().publish(_ТИКЕТЫ_КАНАЛ, {"cmd": "cancel", "ticket": ticket}))
    except RuntimeError:
        pass


def _run_finished(run_id: str) -> bool:
    if not run_id:
        return False
    from finengine.skills import run_orchestrator as _ro  # noqa: PLC0415
    st = _ro.get_run(run_id)
    return st is not None and st.finished


class ToolCallBody(BaseModel):
    tool: str
    args: dict = {}
    gateway_session: str = ""
    # Тикет-протокол долгих тулов: "start" | "poll" | "" (синхронно).
    mode: str = ""
    ticket: str = ""
    # Пилот: плагин шлёт ключи своего runtime-context — учимся, что доступно.
    context_probe: dict = {}


@router.post("/internal/tools/call")
async def tools_call(body: ToolCallBody, authorization: str = Header(default="")):
    from finengine.api.internal_auth import require_gateway_token  # noqa: PLC0415

    require_gateway_token(authorization)

    # Поллинг тикета: сюда приходим без tool-валидации — тикет уже проверен.
    if body.mode == "poll":
        _purge_tickets()
        # Читаем ОБЩУЮ запись, а не локальный словарь: поллинг приходит на
        # любую реплику, а задача живёт у той, что начинала.
        запись = _тикет(body.ticket)
        if запись is None:
            raise HTTPException(status_code=404, detail="unknown ticket")
        if _run_finished(запись.get("run_id") or ""):
            # «Стоп»: ход отменён — гасим и in-flight задачу тула, где бы она
            # ни была.
            _погасить_тикет(body.ticket)
            raise HTTPException(status_code=409,
                                detail="ход отменён — инструменты отключены")
        if not запись.get("done"):
            return JSONResponse({"status": "running"}, status_code=202)
        _tickets.pop(body.ticket, None)
        if запись.get("cancelled"):
            raise HTTPException(status_code=409, detail="ход отменён")
        return запись.get("out") or {"ok": False, "error": "результат тула потерян"}

    # Реальные тулы — только при известном маппинге сессии (owner-скоуп).
    ctx = _ctx_get(body.gateway_session)
    if ctx is None:
        raise HTTPException(status_code=403,
                            detail="unknown gateway session (ход не зарегистрирован)")
    from finengine.llm import tools as _tools  # noqa: PLC0415
    tool_def = _tools.REGISTRY.get(body.tool)
    allowed = _tools._WHITELIST is None or body.tool in _tools._WHITELIST
    if tool_def is None or not allowed:
        raise HTTPException(status_code=400, detail=f"unknown/disabled tool {body.tool!r}")

    # Стоп-семантика гейтвей-петли: ход отменён (cancel_run → finished) →
    # инструменты отключаются. Серверную петлю гейтвея мы не прерываем, но без
    # тулов она быстро доходит до финального текста в закрытый стрим.
    if _run_finished(ctx.get("run_id") or ""):
        raise HTTPException(status_code=409,
                            detail="ход отменён — инструменты отключены")

    # #235: отмечаем ФАКТ вызова здесь — это единственная точка, общая для
    # обоих режимов и для гейтвей-петли, где тулы зовёт не раннер.
    def _отметить(было: dict | None) -> dict:
        запись = dict(было or ctx)
        запись["tools_called"] = [*(запись.get("tools_called") or []), body.tool]
        return запись

    _ctx_mutate(body.gateway_session, _отметить)

    if body.mode == "start":
        _purge_tickets()
        запустить_слушателя()
        ticket = uuid.uuid4().hex
        # Общая запись — ДО задачи: поллинг может прийти раньше, чем задача
        # успеет что-нибудь сделать, и обязан получить «running», а не 404.
        _записать_тикет(ticket, run_id=ctx.get("run_id") or "", done=False)
        run_id = ctx.get("run_id") or ""
        if not run_id or _ход_здесь(run_id):
            _начать_локально(ticket, body.tool, body.args or {}, ctx,
                             body.gateway_session)
            return {"status": "started", "ticket": ticket}

        # 🔴 ХОД НА ДРУГОЙ РЕПЛИКЕ — И ТУЛ ОБЯЗАН ИСПОЛНИТЬСЯ ТАМ ЖЕ.
        # Причина не в удобстве: тул дописывает историю сессии, а `Session` —
        # in-process объект раннера. Исполненный здесь, он допишет в СВОЮ
        # копию, которую никто не сохранит: следующий ход не увидит ни вызова,
        # ни результата, и модель либо повторит работу, либо ответит без
        # данных. Отказа при этом не будет ни одного.
        #
        # Поэтому работа уезжает владельцу через шину, а исход возвращается
        # общей записью тикета — поллинг читает её откуда угодно.
        from finengine.store.bus import get_bus  # noqa: PLC0415

        await get_bus().publish(_ТИКЕТЫ_КАНАЛ, {
            "cmd": "run_tool", "ticket": ticket, "tool": body.tool,
            "args": body.args or {}, "ctx": ctx,
            "gateway_session": body.gateway_session,
        })
        return {"status": "started", "ticket": ticket}

    # Синхронный режим (совместимость: тесты, лёгкие вызовы без mode).
    return await _execute_tool(body.tool, body.args or {}, ctx, body.gateway_session)
