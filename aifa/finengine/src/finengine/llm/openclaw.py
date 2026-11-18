"""OpenClaw client — drop-in replacement for finengine.llm.openrouter.

Routes inference through the OpenClaw gateway HTTP API (`/v1/responses`)
running on the same host. The gateway picks the model and thinking level
from its own config — currently `openrouter/moonshotai/kimi-k2.5` and
`thinkingDefault=max`. This wrapper does NOT pass model/thinking
overrides per call; gateway is the single source of truth there.

Surface kept compatible with `openrouter.py` so callers switch via:

    from finengine.llm import openclaw as llm  # was: openrouter as llm

Most of openrouter's per-request determinism knobs (seed, temperature,
pin_provider) are accepted but ignored — they don't apply when the
gateway agent loop owns the model selection. `reasoning_effort` is
forwarded through `set_reasoning_effort` / per-call kwarg as the
`reasoning.effort` field; gateway's `thinkingDefault` is used otherwise.
"""
from __future__ import annotations

import asyncio
import contextvars as _cv
import json
import logging
import time
from typing import TYPE_CHECKING, AsyncIterator

import httpx

from finengine.config import settings

if TYPE_CHECKING:
    from finengine.llm.tools import ToolContext

log = logging.getLogger(__name__)

# thinking=max can spend a lot of reasoning tokens before emitting any
# output, especially on long contexts. 10 min ceiling matches the gateway
# `agents.defaults.timeoutSeconds=300` plus padding for network and
# OpenRouter upstream variance.
_TIMEOUT = 600.0
_RETRIES = 3

_REASONING_CTX: _cv.ContextVar[str | None] = _cv.ContextVar(
    "finengine_openclaw_reasoning_effort", default=None
)


def set_reasoning_effort(effort: str | None) -> None:
    """Per-task reasoning override. Stored in contextvar (asyncio task-local)
    so concurrent requests don't bleed."""
    if effort and effort.lower() in ("low", "medium", "high", "max", "xhigh", "off"):
        _REASONING_CTX.set(effort.lower())
    else:
        _REASONING_CTX.set(None)


# Тумблер «Поиск в интернете» (меню «+» композера). Contextvar, а не параметр:
# сигнатура query_stream→complete_stream длинная, и тащить флаг через все слои
# ради одного if — шум (тот же приём, что budget.py). Default True — прежнее
# поведение для всех путей, которые тумблер не выставляют (сценарии, извлечение).
_WEB_SEARCH_CTX: _cv.ContextVar[bool] = _cv.ContextVar(
    "finengine_openclaw_web_search", default=True
)
_WEB_TOOLS = {"tavily_search", "fetch_url"}


def set_web_search(enabled: bool) -> None:
    """Разрешить/запретить веб-инструменты модели на текущий ход (task-local)."""
    _WEB_SEARCH_CTX.set(bool(enabled))


def web_search_allowed() -> bool:
    """Разрешён ли выход наружу на этом ходу. Читают НЕ только веб-инструменты
    модели: наружу теперь ходит и сбор внешнего контекста (#159), а тумблер один
    на пользователя и означает одно — согласие."""
    return bool(_WEB_SEARCH_CTX.get())


def _gateway_url() -> str:
    base = (settings.openclaw_gateway_url or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "FINENGINE_OPENCLAW_GATEWAY_URL is not set — required to reach "
            "the OpenClaw gateway. Set it in the deploy .env."
        )
    return base


def _headers() -> dict:
    if not settings.openclaw_gateway_token:
        raise RuntimeError(
            "FINENGINE_OPENCLAW_GATEWAY_TOKEN is not set — required to "
            "authenticate against the OpenClaw gateway."
        )
    return {
        "Authorization": f"Bearer {settings.openclaw_gateway_token}",
        "Content-Type": "application/json",
    }


def _build_input(prompt: str, system: str | None) -> str:
    """Compose user prompt for the gateway's `input` string field.

    We DO NOT inject the caller's `system` text. OpenClaw brings its own
    agent identity, skill catalog and tool descriptions via the
    embedded-agent bootstrap — overriding it with a Finengine-era
    narrow-domain system prompt was making the agent ignore its skills
    and refuse non-finance questions ("приложите PDF, я работаю только
    с отчётностью"). The system parameter is kept on the signature for
    drop-in API compatibility with openrouter.py but ignored here.
    """
    return prompt


# Operating procedure — короткое сообщение роли `developer`, уезжающее КАЖДЫМ
# ходом (после SAFETY, перед сообщением пользователя): кто мы, чего не обещаем,
# как отвечаем. Намеренно НЕ финансовая персона — тяжёлый доменный промпт уже
# заставлял модель отказываться от нефинансовых вопросов (см. _build_input).
#
# МАРШРУТИЗАЦИИ ЗДЕСЬ НЕТ — И БЫТЬ НЕ ДОЛЖНО (#237, снято 14.08.2026).
#
# До 14.08 тут стояла «Карта «намерение → инструмент»»: девять строк вида
# «новости → daily_news_digest», «отчёт + просьба о выводах → разбор».
# Она уезжала в промпт каждым ходом — в том числе в гейтвей-петле, потому что
# `items` собирается ДО проверки ключа сессии, — и решала за модель раньше, чем
# та смотрела на описания. Это и есть ответ по #199: тело навыка не читалось,
# потому что маршрут уже был выдан готовым, а `description` навыков не решал
# ничего. Конструктор навыков был собран и обесточен одной этой врезкой.
#
# Где теперь живёт то, что она говорила: что инструмент умеет и когда его брать —
# в его `description` (llm/tools.py, оттуда генерится и плагин гейтвея); что
# умеет навык — в `description` его frontmatter (ядро кладёт имя+описание в
# системный промпт, тело модель берёт `read`-ом); порядок работы — в теле навыка;
# правило «выбрал навык — читай его тело» — в конституции воркспейса
# (agent-workspace/bootstrap/AGENTS.md). Канон: docs/SKILLS_ARCHITECTURE.md.
_OPERATING_PROCEDURE = (
    "# Кто ты\n\n"
    "Ты — **AIFA**, универсальный агент финансового анализа: работа с отчётностью, "
    "аналитика и финансовое моделирование. Так и представляйся, если спрашивают.\n"
    "Никогда не называй себя «FA-CLAW», «CLAW» или «IR-агентом»: первое — внутреннее имя "
    "стенда, второе — лишь один из сценариев (календарь событий, дайджест), а не суть "
    "продукта. Не перечисляй внутренние имена инструментов (`fe_*`, `ir_*`) — говори о "
    "том, что умеешь делать, а не чем это устроено.\n"
    "Отвечая «что ты умеешь», уложись в 4-6 коротких пунктов без вложенных списков и "
    "эмодзи-заголовков: разбор отчётности и выводы, извлечение показателей в таблицу и "
    "Excel, расчёты и коэффициенты, сравнение периодов и компаний, вопросы по цифрам "
    "загруженного отчёта. Без рекламных прилагательных.\n\n"
    "# Как работать (operating procedure)\n\n"
    "Понимай НАМЕРЕНИЕ пользователя по смыслу запроса и САМ выбирай, чем его выполнить, — "
    "по ОПИСАНИЯМ инструментов и навыков. Готового маршрута тебе здесь никто не даёт: "
    "описание инструмента говорит, что он умеет, описание навыка — какую работу он закрывает. "
    "Это обычный agent-loop, а не список правил. На нефинансовые вопросы тоже отвечай — "
    "не отказывайся.\n\n"
    "Уточняй через `ask_user` РЕДКО: только если трактовка реально неоднозначна и дефолта нет, "
    "либо операция дорогая или необратимая. Если отчёт уже приложен — НЕ спрашивай, какой "
    "документ, период, метрику или вид анализа брать: дефолт — ВСЕ доступные периоды отчёта, полное "
    "покрытие, базовый вид анализа. По мелочам не переспрашивай.\n\n"
    "Если запрос выглядит как опечатка или обрывок, трактуй его В КОНТЕКСТЕ БЕСЕДЫ и не "
    "запускай по догадке долгую работу — либо ответь по очевидной поправке, либо переспроси "
    "одним коротким вопросом.\n\n"
    "Результаты инструментов отдавай пользователю как есть (система сама рендерит "
    "таблицы/отчёты/дайджесты). Не выдумывай числа — бери из инструментов и контекста.\n\n"
    # 🔴 ЖИВОЙ ПРОГОН 23.08.2026, `severstal-Q1-2025.pdf`. Модели не хватило
    # совокупных активов (она сама не включила их в список извлечения), и она
    # написала: «значение взято из баланса отчёта (стр. 7: 536 335 + 331 529)» —
    # то есть СЛОЖИЛА два числа из PDF и подставила результат в расчёт ROCE.
    #
    # Прежняя формулировка этого не запрещала, и по делу: «не выдумывай» — про
    # выдумку, а тут не выдумка; «бери из контекста» — прямое разрешение взять
    # из текста отчёта. Запрет нужен не на чтение отчёта (спросили «что на
    # странице 42» — читай и отвечай), а на ПОДСТАНОВКУ добытого вручную числа
    # в расчёт или таблицу вместо непришедшего показателя: там оно неотличимо
    # от извлечённого, ничем не подтверждено и никем не проверено.
    "Числа, на которых стоит РАСЧЁТ или ТАБЛИЦА, берутся только у инструментов. "
    "Показателя не оказалось — так и скажи («такого-то нет в извлечении») или "
    "запроси его отдельно; складывать, вычитать и вычитывать его из текста отчёта "
    "самому НЕЛЬЗЯ, даже когда нужное число видно в PDF и арифметика очевидна. "
    "Прямой вопрос по документу («что написано на странице 42») это не запрещает: "
    "там ты отвечаешь по тексту и говоришь, откуда взял.\n\n"
    "Стиль: не описывай свои шаги по нескольку раз и не повторяй одну и ту же мысль "
    "(«Провожу анализ… Провожу анализ на основе…»). Если работа долгая — допустимо ОДНО "
    "короткое уведомление о начале; финальный ответ начинай сразу с сути, без вводной воды."
)


# Имена видов анализа инструкция больше НЕ называет (#237): их единственный
# источник для модели — описания навыков, которые ядро кладёт в системный
# промпт блоком `<available_skills>`. Прежняя подстановка
# `{analysis_types}` жила здесь как страховка от вписанного руками имени (#148);
# страховка нужна по-прежнему, но сторожить надо описание тула, а не инструкцию.


def _resolve_reasoning(reasoning_effort: str | None) -> str | None:
    """Resolve effort: per-call > contextvar > None (gateway uses its
    own thinkingDefault)."""
    eff = (reasoning_effort or _REASONING_CTX.get() or "").lower() or None
    if eff in ("low", "medium", "high", "max", "xhigh", "off"):
        return eff
    return None


def _extract_text(resp: dict) -> str:
    """Pull final assistant text out of the gateway response payload.

    Gateway returns OpenAI Responses API shape:
      {
        "output": [{"type":"message", "content":[{"type":"output_text","text":"..."}]}],
        "output_text": "...",  # convenience
        ...
      }
    """
    text = resp.get("output_text") or ""
    if text:
        return text
    # 🔴 СООБЩЕНИЯ РАЗДЕЛЯЮТСЯ ПУСТОЙ СТРОКОЙ, А НЕ СКЛЕИВАЮТСЯ (20.08.2026).
    #
    # `output` несёт НЕСКОЛЬКО сообщений модели: одно до вызова инструмента,
    # другое после, третье в конце. Раньше их тексты складывались подряд без
    # единого знака между ними, и на экране выходило «и сразу распакую
    # полХорошо, отчёт классифицирован» — модель оборвала фразу на слове
    # «полку», позвала инструмент и начала новое сообщение. Пользователь видел
    # слипшиеся слова и читал это как сбой агента (жалоба владельца 20.08:
    # «агент явно криво пишет свои размышления»).
    #
    # Внутри ОДНОГО сообщения куски — это поток одной фразы, их клеим подряд.
    # Между сообщениями ставим пустую строку: это разные высказывания, и
    # markdown отобразит их разными абзацами.
    blocks: list[str] = []
    for item in resp.get("output", []) or []:
        if item.get("type") != "message":
            continue
        part = ""
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text"):
                part += c.get("text", "") or ""
        if part.strip():
            blocks.append(part.strip())
    return "\n\n".join(blocks)


def _extract_usage(resp: dict) -> dict:
    raw = resp.get("usage", {}) or {}
    cached = 0
    details = raw.get("input_tokens_details")
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens", 0) or 0)
    return {
        "in": int(raw.get("input_tokens", 0) or 0),
        "out": int(raw.get("output_tokens", 0) or 0),
        "cached": cached,
        "model": resp.get("model", "") or "",
    }


def _tools_mark(gateway_session_key: str | None) -> str:
    """Чем модель РЕАЛЬНО работала за этот ход — для отметки в логе (#235).

    В гейтвей-петле тулы исполняет гейтвей, раннер их не видит, и прежняя
    отметка печатала количество ОСТАВШИХСЯ вызовов под именем `tools=` — на
    финальном ходе оно ноль по определению, то есть «инструментов не было»
    писалось всегда. Повторы не схлопываем: два `fe_analyze` подряд — это
    диагноз, а не шум.
    """
    if not gateway_session_key:
        return "инлайн"
    # Ленивый импорт: `api.internal_tools` тянет `llm.tools`, верхний импорт
    # обратно замкнул бы цикл.
    from finengine.api.internal_tools import tools_called_of  # noqa: PLC0415
    called = tools_called_of(gateway_session_key)
    return ",".join(called) if called else "нет"


async def complete_with_usage(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    retries: int = _RETRIES,
    response_format: dict | None = None,
    max_output: int | None = None,
    *,
    seed: int | None = None,
    temperature: float | None = None,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, dict]:
    """Non-streaming inference through gateway. Returns (text, usage).

    `model`, `seed`, `temperature`, `pin_provider` are accepted for API
    parity but ignored — gateway owns these.
    """
    payload: dict = {
        "model": "openclaw/default",
        "input": _build_input(prompt, system),
    }
    if max_output:
        payload["max_output_tokens"] = max_output
    eff = _resolve_reasoning(reasoning_effort)
    if eff:
        payload["reasoning"] = {"effort": eff}
    if response_format:
        payload["response_format"] = response_format

    url = f"{_gateway_url()}/v1/responses"
    headers = _headers()
    last_err: Exception | None = None

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in range(retries):
            try:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = _extract_text(data)
                usage = _extract_usage(data)
                return text, usage
            except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as e:
                last_err = e
                log.warning(
                    "OpenClaw call failed (attempt %d/%d): %s",
                    attempt + 1, retries, e,
                )
                await asyncio.sleep(2 * (attempt + 1))

    if last_err:
        raise last_err
    return "", {"in": 0, "out": 0, "cached": 0, "model": ""}


async def complete(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    retries: int = _RETRIES,
    response_format: dict | None = None,
    max_output: int | None = None,
    *,
    seed: int | None = None,
    temperature: float | None = None,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    text, _ = await complete_with_usage(
        prompt=prompt, model=model, system=system, retries=retries,
        response_format=response_format, max_output=max_output,
        seed=seed, temperature=temperature, pin_provider=pin_provider,
        reasoning_effort=reasoning_effort,
    )
    return text


async def stream(
    prompt: str,
    system_prompt: str | None = None,
    model: str | None = None,
    response_format: dict | None = None,
    max_output: int | None = None,
    usage_sink: dict | None = None,
    reasoning_effort: str | None = None,
) -> AsyncIterator[str]:
    """Streaming inference via SSE on `/v1/responses` with stream=true.

    Yields text deltas. If `usage_sink` is provided, fills it in-place
    with the final {"in","out","cached","model"} from the response.completed
    event.

    Gateway streams OpenAI Responses API event types — we care about:
      - `response.output_text.delta` { "delta": "..." }
      - `response.completed` { "response": { "usage": {...}, "model": "..." } }
    Other event types (reasoning deltas, tool-use, etc.) are ignored — they
    produce no user-visible text in this minimal wrapper.
    """
    payload: dict = {
        "model": "openclaw/default",
        "input": _build_input(prompt, system_prompt),
        "stream": True,
    }
    if max_output:
        payload["max_output_tokens"] = max_output
    eff = _resolve_reasoning(reasoning_effort)
    if eff:
        payload["reasoning"] = {"effort": eff}
    if response_format:
        payload["response_format"] = response_format

    url = f"{_gateway_url()}/v1/responses"
    headers = _headers()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                ev_type = ev.get("type", "")
                if ev_type == "response.output_text.delta":
                    delta = ev.get("delta") or ""
                    if delta:
                        yield delta
                elif ev_type == "response.completed":
                    if usage_sink is not None:
                        body = ev.get("response") or {}
                        u = body.get("usage") or {}
                        cached = 0
                        det = u.get("input_tokens_details")
                        if isinstance(det, dict):
                            cached = int(det.get("cached_tokens", 0) or 0)
                        usage_sink["in"] = int(u.get("input_tokens", 0) or 0)
                        usage_sink["out"] = int(u.get("output_tokens", 0) or 0)
                        usage_sink["cached"] = cached
                        usage_sink["model"] = body.get("model", "") or ""


_PLAN_TOOL = "update_plan"
# Как ядро называет состояния шага. Наш экран знает три своих: running/done/error.
_PLAN_STATUS = {"pending": "running", "in_progress": "running",
                "completed": "done", "done": "done",
                "blocked": "error", "failed": "error", "error": "error"}


def _spectator_plan_event(item: dict) -> "list[dict] | None":
    """Ядровой `update_plan` → события экрана. `None`, если это не он.

    🔴 ЗАЧЕМ. План хода — это состояние РАЗГОВОРА, а не работы в комнате. Наш
    `station-skills/_runner/plan.py` держит его в станции и шлёт по каналу
    нонса; ядро умеет то же самое своим тулом, и его системный промпт вдобавок
    требует держать ровно один шаг «в работе» и не повторять план целиком —
    ровно то поведение, которого мы от своего скрипта добиться не можем.

    ⚠️ ФОРМУ АРГУМЕНТОВ ЯДРА Я НЕ ЗНАЮ. Тул экспериментальный
    (`tools.experimental.planTool`), схемы в бандле нет, документация её не
    приводит. Поэтому здесь разбор ПО НЕСКОЛЬКИМ правдоподобным формам, а
    незнакомая — не молча игнорируется, а печатается в лог целиком: первый же
    живой вызов покажет правду, и тогда разбор сведётся к одной ветке.

    Гадать и зашивать одну форму было бы хуже: несовпадение выглядело бы как
    «план не пришёл», и искать причину пришлось бы глазами.
    """
    if str(item.get("name") or "") != _PLAN_TOOL:
        return None
    try:
        args = json.loads(item.get("arguments") or "{}")
    except json.JSONDecodeError:
        log.info("update_plan: аргументы не разобрались как JSON: %r",
                 str(item.get("arguments"))[:400])
        return []
    if not isinstance(args, dict):
        return []

    # Список шагов лежит под одним из этих имён — какое именно, покажет жизнь.
    шаги = None
    for ключ in ("plan", "steps", "todos", "items"):
        значение = args.get(ключ)
        if isinstance(значение, list):
            шаги = значение
            break
    if шаги is None:
        log.info("update_plan: незнакомая форма аргументов, ключи=%s, целиком=%r",
                 sorted(args), json.dumps(args, ensure_ascii=False)[:400])
        return []

    названия: list[str] = []
    статусы: list[str] = []
    for ш in шаги:
        if isinstance(ш, str):
            названия.append(ш)
            статусы.append("")
            continue
        if not isinstance(ш, dict):
            continue
        имя = ш.get("step") or ш.get("title") or ш.get("content") or ш.get("text") or ""
        названия.append(str(имя))
        статусы.append(_PLAN_STATUS.get(str(ш.get("status") or "").lower(), ""))

    out: list[dict] = [{"type": "plan", "name": _PLAN_TOOL, "steps": названия}]
    # Отмечаем ТОЛЬКО те шаги, у которых состояние названо. Ядро требует
    # держать ровно один «в работе», но полагаться на это нельзя: пустое
    # состояние = «ничего не говорю про этот шаг», а не «он не начат».
    for i, st in enumerate(статусы):
        if st:
            out.append({"type": "plan_step", "name": _PLAN_TOOL,
                        "index": i, "status": st})
    return out


def _progress_events(item: object, tool_name: str) -> list[dict]:
    """Convert one ctx.progress queue item into SSE events.

    Plain strings (legacy) → a `tool_progress` line. Dicts are the structured
    protocol for the chat «Прогресс» island:
      {"plan": [...]}                → plan (declare the checklist)
      {"step": i, "status", "note"?} → plan_step (+ tool_progress with the note
                                        so the ephemeral thinking line updates)
    """
    if isinstance(item, dict) and "plan" in item:
        return [{
            "type": "plan", "name": tool_name,
            "steps": [str(s) for s in (item.get("plan") or [])],
        }]
    if isinstance(item, dict) and "extract_start" in item:
        return [{"type": "extract_start", "name": tool_name, **(item["extract_start"] or {})}]
    if isinstance(item, dict) and "metric_row" in item:
        return [{"type": "metric_row", "name": tool_name, "metric": item["metric_row"] or {}}]
    if isinstance(item, dict) and "extract_done" in item:
        return [{"type": "extract_done", "name": tool_name, **(item["extract_done"] or {})}]
    if isinstance(item, dict) and "extract_error" in item:
        return [{"type": "extract_error", "name": tool_name, "message": str(item["extract_error"])}]
    if isinstance(item, dict) and "step" in item:
        ev: dict = {
            "type": "plan_step", "name": tool_name,
            "index": int(item.get("step") or 0),
            "status": str(item.get("status") or "running"),
        }
        note = item.get("note")
        out: list[dict] = [ev]
        if note:
            ev["note"] = str(note)
            out.append({"type": "tool_progress", "name": tool_name, "text": str(note)})
        return out
    return [{"type": "tool_progress", "name": tool_name, "text": str(item)}]


async def _exec_tool_streaming(name: str, args: dict, ctx) -> "AsyncIterator[dict]":
    """Execute a tool while draining its ctx.progress queue into client events.

    A long tool (a full IR-Calendar scan is minutes) otherwise yields NOTHING
    between tool_call and tool_output, so the SSE idle-times-out ("load
    failed"). Draining keeps bytes flowing and feeds the «Прогресс» island.

    Yields progress/plan/metric-row events as they arrive; the FINAL item is
    the internal sentinel {"__tool_result__": output_str} — the caller consumes
    it and must NOT forward it to the client. A tool exception propagates out
    of the generator (same as the old inline `exec_task.result()`)."""
    from finengine.llm import tools as _tools  # noqa: PLC0415
    ctx.progress = asyncio.Queue()
    exec_task = asyncio.create_task(_tools.execute(name, args, ctx))
    try:
        while not exec_task.done():
            getter = asyncio.create_task(ctx.progress.get())
            done_set, _pending = await asyncio.wait(
                {exec_task, getter}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter in done_set:
                for _pe in _progress_events(getter.result(), name):
                    yield _pe
            else:
                getter.cancel()
        while not ctx.progress.empty():
            for _pe in _progress_events(ctx.progress.get_nowait(), name):
                yield _pe
        yield {"__tool_result__": exec_task.result()}
    finally:
        ctx.progress = None


# ─────────────────── tool-aware streaming ────────────────────


async def stream_events(
    prompt: str,
    *,
    tool_ctx: "ToolContext | None" = None,
    enable_tools: bool = True,
    max_tool_iters: int = 7,
    max_output: int | None = None,
    reasoning_effort: str | None = None,
    pinned_tool: str | None = None,
    pinned_tool_args: dict | None = None,
    gateway_session_key: str | None = None,
) -> "AsyncIterator[dict]":
    """Tool-aware streaming generator.

    #98 C3-2: `gateway_session_key` включает ГЕЙТВЕЙ-ПЕТЛЮ — мы не шлём
    client-tools и не крутим локальный цикл: гейтвей ведёт петлю сам (нативные
    + плагин fe-tools), ключ сессии задаётся заголовком x-openclaw-session-key,
    по нему бэк на /internal/tools/call находит владельца хода. Чипы тулов и
    прогресс в этом режиме эмитит бэкенд напрямую в chat-ран (не этот стрим).

    Yields a sequence of typed events the FastAPI handler can wrap as SSE:
        {"type": "text_delta", "text": "..."}
        {"type": "tool_call",  "name": "...", "args": {...}}
        {"type": "tool_output","name": "...", "output": "<short str>"}
        {"type": "done",       "usage": {...}}
        {"type": "error",      "message": "..."}

    Multi-turn loop: when the agent emits a function_call, we run the
    handler and re-open the stream with the full history including the
    tool output. Caps at `max_tool_iters` to avoid runaway loops.
    """
    from finengine.llm import tools as _tools  # noqa: PLC0415
    from finengine import policy as _policy  # noqa: PLC0415
    from finengine.policy.guardrails import StreamScrubber, scrub  # noqa: PLC0415

    ctx = tool_ctx or _tools.ToolContext()
    # Output guardrail. Buffers ~100 chars at the tail of each delta
    # stream so API-key / internal-path patterns spanning a chunk
    # boundary still match. Reset on each tool-turn boundary so the
    # buffer doesn't carry across turns.
    scrubber = StreamScrubber()
    # SAFETY block is the FIRST item every turn — see
    # finengine/policy/__init__.py for the canonical text. Even if the
    # agent's workspace POLICY.md gets tampered with, this in-code
    # injection still makes the rules reach the LLM.
    items: list[dict] = [
        _policy.safety_message_item(),
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": _OPERATING_PROCEDURE}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        },
    ]

    # Accumulate usage across ALL turns of the tool-loop — one user turn
    # may trigger up to max_tool_iters LLM calls, each billed separately.
    # Reporting only the last turn's usage hides ~80% of the burn.
    usage_total = {"in": 0, "out": 0, "cached": 0, "model": "", "turns": 0}

    url = f"{_gateway_url()}/v1/responses"
    headers = _headers()
    if gateway_session_key:
        # Гейтвей-петля: наш ключ сессии + никаких client-tools в payload.
        headers = {**headers, "x-openclaw-session-key": gateway_session_key}
        enable_tools = False

    from finengine import observability as _obs  # noqa: PLC0415
    _trace_uid = ctx.user_id
    _trace_sid = ctx.session_id

    # Tool-call de-dup cache: (tool_name, json_args) → previous output.
    # Models stuck in a loop sometimes re-fire the SAME query for 4+ turns
    # in a row (saw DeepSeek do this on a digest). Short-circuit by
    # returning the cached result with an explicit hint to vary inputs.
    _dedup_cache: dict[tuple[str, str], str] = {}
    # Счётчик ОТКАЗОВ по инструменту (решение владельца 17.08: «три ретрая»).
    #
    # Кэш выше сверяет ТОЧНЫЕ аргументы, и в живом ходе он не помог: модель
    # девять раз звала tavily_search, каждый раз чуть переформулировав запрос
    # («Claude AI acquired Uber shares stocks 2026», «Claude Anthropic Uber»…).
    # Инструмент каждый раз возвращал один и тот же внятный отказ, а ход умер
    # по общему лимиту ходов — пользователь вместо «поиск не работает» увидел
    # «agent ran more than 7 tool turns — aborted».
    #
    # Считаем ОТКАЗЫ инструмента независимо от аргументов: три — и дальше он в
    # этом ходе не зовётся, а модели говорится словами, что делать.
    _tool_failures: dict[str, int] = {}
    _MAX_TOOL_FAILURES = 3

    for turn in range(max_tool_iters + 1):
        # #118: развесовка хода по секундам. Замер Nestlé показал 8 минут ПОСЛЕ
        # последнего вызова модели, и по логам было не понять, где они: внутри
        # стрима гейтвея, между стримом и нашим циклом или в тулах. Три отметки
        # (старт хода / первый байт от гейтвея / конец хода) закрывают вопрос на
        # первом же живом прогоне, без догадок.
        _turn_t0 = time.perf_counter()
        _turn_first_byte: float | None = None
        payload: dict = {
            "model": "openclaw/default",
            "input": items,
            "stream": True,
        }
        _obs.emit(
            user_id=_trace_uid, session_id=_trace_sid,
            event="llm_turn_start",
            turn=turn,
            items_count=len(items),
            payload_chars=sum(
                len(json.dumps(it, ensure_ascii=False, default=str)) for it in items
            ),
        )
        if enable_tools:
            defs = _tools.all_definitions()
            if not _WEB_SEARCH_CTX.get():
                defs = [d for d in defs if d.get("name") not in _WEB_TOOLS]
            payload["tools"] = defs
            # МОДЕЛЬ ВХОДОВ: a shortcut button pins the FIRST turn's decision
            # (OpenResponses function pinning) — the gateway must call this
            # client tool. Later turns run auto, otherwise the forced choice
            # would loop the same tool forever.
            if pinned_tool and turn == 0:
                payload["tool_choice"] = {"type": "function", "name": pinned_tool}
        if max_output:
            payload["max_output_tokens"] = max_output
        eff = _resolve_reasoning(reasoning_effort)
        if eff:
            payload["reasoning"] = {"effort": eff}

        # Per-stream accumulators — fresh each turn.
        pending_calls: dict[str, dict] = {}  # call_id → {name, args_str}
        any_event_this_turn = False
        completed_seen = False

        # Гейтвей-петля: read-таймаут клиента обязан переживать молчание SSE во
        # время долгого серверного тула (пер-строчный дедлайн ниже — 45 мин),
        # иначе httpx.ReadTimeout убивает ход на _TIMEOUT (грабля: «network: »
        # с пустым текстом за секунды до финиша ~10-минутного календаря).
        _client_timeout = (
            httpx.Timeout(connect=30.0, read=2700.0, write=60.0, pool=30.0)
            if gateway_session_key else _TIMEOUT
        )
        async with httpx.AsyncClient(timeout=_client_timeout) as client:
            try:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        yield {
                            "type": "error",
                            "message": f"gateway {resp.status_code}: {body[:200]!r}",
                        }
                        return
                    # Manual iteration so the FIRST line gets its own deadline:
                    # when the gateway's model upstream rejects the call (e.g.
                    # openrouter WAF 403) the gateway can go silent without
                    # closing the stream — without this we'd sit the full
                    # _TIMEOUT (10 min) with zero feedback.
                    _line_iter = resp.aiter_lines()
                    _got_first = False
                    # Гейтвей-петля: пока гейтвей исполняет долгий тул серверно,
                    # SSE легально МОЛЧИТ (keepalive-ов нет; календарь/extract —
                    # до ~10+ мин; хартбит хода — поллы плагина в наш бэк).
                    # Обычный (инлайн) режим оставляем на строгом _TIMEOUT.
                    _line_deadline = 2700.0 if gateway_session_key else _TIMEOUT
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                _line_iter.__anext__(),
                                timeout=(120.0 if not _got_first else _line_deadline),
                            )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            yield {
                                "type": "error",
                                "message": (
                                    (
                                        "Модельный шлюз не ответил за 2 минуты — "
                                        "похоже, upstream отклонил запрос (security "
                                        "policy/лимит). Попробуйте ещё раз."
                                    )
                                    if not _got_first
                                    else (
                                        "Поток модельного шлюза замолчал дольше "
                                        f"{int(_line_deadline // 60)} мин — ход "
                                        "прерван как зависший."
                                    )
                                ),
                            }
                            return
                        if not _got_first and _turn_first_byte is None:
                            _turn_first_byte = time.perf_counter() - _turn_t0
                        _got_first = True
                        if not raw or not raw.startswith("data:"):
                            continue
                        data = raw[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            ev = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        any_event_this_turn = True
                        ev_type = ev.get("type", "")
                        if ev_type in ("response.failed", "response.error"):
                            _err = ev.get("response", {}).get("error") if isinstance(ev.get("response"), dict) else None
                            _err = _err or ev.get("error") or {}
                            _msg = (_err.get("message") if isinstance(_err, dict) else str(_err)) or "response.failed"
                            yield {"type": "error", "message": f"gateway: {_msg}"}
                            return
                        if ev_type == "response.output_text.delta":
                            delta = ev.get("delta") or ""
                            if delta:
                                # Stream LIVE through the scrubber — the user
                                # sees the agent's progress + answer AS it is
                                # produced, not a multi-minute hang followed by
                                # a wall of text dumped at once. The gateway runs
                                # the whole tool-loop server-side, so there is no
                                # local "draft 1/2/3" to suppress here; the old
                                # buffering only delayed every byte to the end.
                                safe = scrubber.feed(delta)
                                if safe:
                                    yield {"type": "text_delta", "text": safe}
                        elif ev_type == "response.output_item.done":
                            item = ev.get("item") or {}
                            if item.get("type") == "function_call" and not gateway_session_key:
                                # В гейтвей-петле function_call-итемы (если гейтвей
                                # когда-то начнёт их слать) НЕ исполняем локально —
                                # петля серверная, у нас только зритель.
                                cid = item.get("call_id") or item.get("id") or ""
                                pending_calls[cid] = {
                                    "name": item.get("name", ""),
                                    "args_str": item.get("arguments") or "{}",
                                    "raw": item,
                                }
                            elif item.get("type") == "function_call":
                                # 🔴 ЗРИТЕЛЬ СМОТРИТ, А НЕ ИСПОЛНЯЕТ (27.08.2026).
                                #
                                # Петля серверная, тул уже отработал в гейтвее —
                                # звать его тут нельзя. Но ОДИН ядровой тул нам
                                # интересен как СОБЫТИЕ: `update_plan` несёт план
                                # хода, а план — это то, что видит человек.
                                #
                                # Свой `plan.py` (станционный скрипт) делает ровно
                                # то же самое худшим способом: план живёт в комнате,
                                # хотя он про разговор, и никем не сверяется.
                                #
                                # ⚠️ ПРИХОДЯТ ЛИ ЭТИ ИТЕМЫ ВООБЩЕ — НЕ ПРОВЕРЕНО.
                                # Комментарий выше («если гейтвей когда-то начнёт
                                # их слать») написан без доказательства, и я его не
                                # добыл. Поэтому здесь И перевод, И след: пришло —
                                # увидим в логе поимённо, и только тогда снимем
                                # `plan.py`. Не пришло — эта ветка молчит, и ничего
                                # не ломает.
                                _plan_events = _spectator_plan_event(item)
                                if _plan_events is not None:
                                    log.info("зритель: ядровой тул %s прислал план",
                                             item.get("name", ""))
                                    for _pe in _plan_events:
                                        yield _pe
                                else:
                                    log.debug("зритель: ядровой тул %s (не план)",
                                              item.get("name", ""))
                            elif item.get("type") == "message" and gateway_session_key:
                                # 🔴 ГРАНИЦА АБЗАЦА МОДЕЛИ (31.08.2026, жалоба владельца).
                                #
                                # Модель пишет текст ПОРЦИЯМИ: сказала, что будет
                                # делать → сходила в exec → сказала дальше. Сами
                                # вызовы exec до нас не доходят (function_call-итемов
                                # в потоке нет — проверено логом), поэтому порции
                                # склеивались в один абзац без пробелов
                                # («...расчёта.Отчёт — НОВАТЭК...») и рассуждения по
                                # дороге выглядели финальным ответом.
                                #
                                # Но у КАЖДОЙ порции есть свой item type="message",
                                # и его завершение — единственная граница, которую
                                # гейтвей нам отдаёт. Шлём её фронту: он откладывает
                                # написанное в ленту хода, а ответом остаётся текст
                                # ПОСЛЕДНЕЙ порции. Приходят ли эти итемы на нашем
                                # ядре — увидим по логу ниже; не приходят — событие
                                # не шлётся и ничего не меняется.
                                log.info("зритель: конец текстовой порции")
                                yield {"type": "text_segment_end"}
                        elif ev_type == "response.completed":
                            completed_seen = True
                            body = ev.get("response") or {}
                            usage = body.get("usage") or {}
                            # Accumulate THIS turn's usage into the running
                            # total — final yield below sends the aggregate.
                            det = usage.get("input_tokens_details") or {}
                            usage_total["in"] += int(usage.get("input_tokens", 0) or 0)
                            usage_total["out"] += int(usage.get("output_tokens", 0) or 0)
                            usage_total["cached"] += int(det.get("cached_tokens", 0) or 0)
                            usage_total["model"] = body.get("model", "") or usage_total["model"]
                            usage_total["turns"] += 1
                            if not pending_calls:
                                # FINAL turn — no more tool calls. The answer
                                # text was already streamed live above; just
                                # flush the scrubber's tail buffer and finish.
                                # #118: успешный текстовый ход выходит ЗДЕСЬ, до
                                # общей отметки внизу цикла — дублируем её,
                                # иначе самые частые ходы не попадают в лог.
                                log.info(
                                    "gw turn=%d stream=%.1fs first_byte=%s model=%s "
                                    "tools=%s completed=True",
                                    turn, time.perf_counter() - _turn_t0,
                                    f"{_turn_first_byte:.1f}s" if _turn_first_byte is not None else "нет",
                                    usage_total["model"] or "?",
                                    _tools_mark(gateway_session_key),
                                )
                                tail = scrubber.flush()
                                if tail:
                                    yield {"type": "text_delta", "text": tail}
                                yield {"type": "done", "usage": dict(usage_total)}
                                return
                            # Tool-loop turn — any text streamed live above was
                            # the agent's between-tools commentary; fine as live
                            # progress. Fall through to run the pending tools.
            except httpx.HTTPError as e:
                # str(e) у httpx.ReadTimeout и родни часто ПУСТОЙ — без класса
                # исключения диагноз по логам невозможен (грабля: «network: »).
                yield {"type": "error",
                       "message": f"network: {type(e).__name__}: {e}"}
                return

        # Stream closed without sending us any events — gateway silent
        # failure. Surface so the UI doesn't sit on "Думаю…" forever.
        if not any_event_this_turn:
            yield {
                "type": "error",
                "message": (
                    "Gateway закрыл поток без событий (возможно конфликт имён "
                    "tools или внутренняя ошибка). Проверьте логи openclaw-gateway."
                ),
            }
            return
        # #118: развесовка хода — печатаем ДО всех return'ов ниже, иначе
        # ходы, закончившиеся текстом (самый частый случай), не попадут в лог.
        log.info(
            "gw turn=%d stream=%.1fs first_byte=%s model=%s tools=%s pending=%d "
            "completed=%s",
            turn, time.perf_counter() - _turn_t0,
            f"{_turn_first_byte:.1f}s" if _turn_first_byte is not None else "нет",
            usage_total["model"] or "?",
            _tools_mark(gateway_session_key),
            len(pending_calls), completed_seen,
        )

        # We got some events but never response.completed AND no
        # function calls — model produced no actionable answer.
        if not pending_calls and not completed_seen:
            yield {
                "type": "error",
                "message": "Gateway оборвал поток до response.completed.",
            }
            return

        # No function calls emitted — just text. Done already yielded.
        if not pending_calls:
            return

        # Flush the text scrubber buffer before running tools so the
        # user sees the agent's commentary text BEFORE the tool_call
        # event arrives. Reset for the next turn.
        tail = scrubber.flush()
        if tail:
            yield {"type": "text_delta", "text": tail}

        # Run each pending tool, append call+output items to history.
        for cid, call in pending_calls.items():
            name = call["name"]
            try:
                args = json.loads(call["args_str"]) if call["args_str"] else {}
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_call", "name": name, "args": args}
            # `ask_user` is a CLARIFY tool: do NOT execute it or feed a result
            # back to the model — hand the question to the user and END the turn.
            # Their reply arrives as the next turn's message (history carries it).
            if name == "ask_user":
                q = str(args.get("question") or "").strip()
                opts = [str(o) for o in (args.get("options") or []) if str(o).strip()][:6]
                _obs.emit(
                    user_id=_trace_uid, session_id=_trace_sid,
                    event="ask_user", turn=turn, tool=name, args=args, call_id=cid,
                )
                yield {"type": "clarify", "question": q, "options": opts}
                yield {"type": "done", "usage": dict(usage_total)}
                return
            _obs.emit(
                user_id=_trace_uid, session_id=_trace_sid,
                event="tool_executing", turn=turn,
                tool=name, args=args, call_id=cid,
            )
            # Dedup: identical (tool, args) inside ONE stream → return
            # cached output + a clear nudge to the model that this is
            # a repeat and it should move on instead of refiring.
            _dedup_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
            _t0 = time.perf_counter()
            if _tool_failures.get(name, 0) >= _MAX_TOOL_FAILURES:
                output_str = json.dumps({
                    "error": "tool_disabled_this_turn",
                    "hint": (f"Инструмент {name} отказал {_MAX_TOOL_FAILURES} раза подряд "
                             "— больше он в этом ходе не вызывается. Не пробуй его снова "
                             "и не подбирай новые формулировки. Ответь пользователю тем, "
                             "что уже знаешь, и ПРЯМО СКАЖИ, что именно не сработало."),
                }, ensure_ascii=False)
                _obs.emit(
                    user_id=_trace_uid, session_id=_trace_sid,
                    event="tool_disabled_after_failures", turn=turn,
                    tool=name, args=args, call_id=cid,
                )
            elif _dedup_key in _dedup_cache:
                output_str = (
                    "{\"warning\": \"duplicate_call_detected\", "
                    "\"hint\": \"You already called this tool with identical "
                    "arguments earlier in this session. Don't repeat — vary the query "
                    "or move on to the next step of the procedure.\", "
                    "\"previous_result_preview\": "
                    + json.dumps(_dedup_cache[_dedup_key][:500], ensure_ascii=False)
                    + "}"
                )
                _obs.emit(
                    user_id=_trace_uid, session_id=_trace_sid,
                    event="tool_dedup_blocked", turn=turn,
                    tool=name, args=args, call_id=cid,
                )
            else:
                # Run the tool, forwarding progress events (see helper).
                output_str = ""
                async for _te in _exec_tool_streaming(name, args, ctx):
                    if "__tool_result__" in _te:
                        output_str = _te["__tool_result__"]
                    else:
                        yield _te
                _dedup_cache[_dedup_key] = output_str
                # Отказом считаем ЯВНЫЙ error в ответе инструмента: наши
                # обработчики возвращают его строкой JSON, а не бросают.
                if '"error"' in output_str[:400]:
                    _tool_failures[name] = _tool_failures.get(name, 0) + 1
            _obs.emit(
                user_id=_trace_uid, session_id=_trace_sid,
                event="tool_result", turn=turn,
                tool=name, call_id=cid,
                output=output_str,
                output_chars=len(output_str),
                latency_ms=round((time.perf_counter() - _t0) * 1000, 1),
            )
            # Scrub tool result preview before showing to user (the
            # full output goes back to the agent unscrubbed — the
            # agent NEEDS the raw data to do its job, but the user-
            # facing preview must be safe).
            # Ш3: честный исход берётся ИЗ КОНВЕРТА тула. Раньше `ok` здесь не
            # эмитилось вовсе, приёмник читал ev.get("ok", True) — и чип был
            # зелёным даже на прогоне, вернувшем ноль показателей. Тулы без
            # конверта (ещё не переведены) поля не получают, поведение прежнее:
            # выдумывать за них статус — вернуть ту же ложь с другой стороны.
            _tool_ev: dict = {
                "type": "tool_output",
                "name": name,
                "output": scrub(output_str[:500]),
                # Full unscrubbed payload — agent layer uses this to
                # short-circuit `daily_news_digest` (and other "pre-rendered"
                # tools) so the LLM doesn't get a chance to rewrite/truncate
                # the canonical markdown. Not shown to the user directly.
                "output_full": output_str,
            }
            _outcome = _tools.outcome_of(output_str)
            if _outcome is not None:
                _status, _reason = _outcome
                _tool_ev["ok"] = _status != _tools.OUTCOME_FAILED
                _tool_ev["status"] = _status
                if _reason:
                    _tool_ev["reason"] = scrub(_reason[:400])
            yield _tool_ev
            # Append a MINIMAL function_call item (gateway rejected the
            # raw one we received with "input: Invalid input" — extra
            # fields like id/status confuse the input parser). Spec:
            # https://platform.openai.com/docs/api-reference/responses
            items.append(
                {
                    "type": "function_call",
                    "call_id": cid,
                    "name": name,
                    "arguments": call["args_str"],
                }
            )
            # Truncate tool output before sending back to the model.
            # Without this each turn re-sends the full output (often 5-50KB
            # of JSON / search results / fetched page text), context grows
            # quadratically, model gets confused and never converges. 2.5KB
            # is enough for the model to know what came back and key
            # snippets, but small enough to stay inside the context budget.
            # Потолок один на все тулы (#166). Прибавка до 14K существовала
            # ради монолита извлечения: он возвращал таблицу на ~75 строк, и её
            # надо было показать в чате. Тула нет; таблицу на пути по навыкам
            # рисует не возврат тула, а канон из нонс-канала
            # (`api/station_turn._stream_canon`), который через этот потолок
            # не проходит вовсе.
            _TOOL_OUT_CAP = 2500
            truncated = output_str
            if len(output_str) > _TOOL_OUT_CAP:
                truncated = (
                    output_str[:_TOOL_OUT_CAP]
                    + f"\n…[truncated: full output was {len(output_str)} chars]"
                )
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": cid,
                    "output": truncated,
                }
            )
        # Continue outer loop for next turn.

    # Loop hit the iter cap.
    yield {
        "type": "error",
        "message": f"agent ran more than {max_tool_iters} tool turns — aborted.",
    }
