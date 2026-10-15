"""Run a scenario end-to-end and persist its output as an artifact.

`run_scenario(scn)` is an async generator of normalized progress events:

    {"type": "started", "kind", "name"}
    {"type": "progress", "text"}        # human status line (0..N)
    {"type": "artifact", "run_id", "kind"}   # on success
    {"type": "done", "run_id"}
    {"type": "error", "message"}        # on failure (generator still returns)

  For `agent_prompt` it also RELAYS the agent's chat events verbatim
  (text_delta / thinking / tool_call / tool_output / sources), so a manual run
  renders as a live chat (tool chips + streaming text); ir_daily/ir_calendar
  relay a `text_delta` with the result. The headless scheduler just drains.

It is the single execution path for BOTH:
  - the scheduler (headless — just drains the generator), and
  - manual "run now" (wrapped in run_orchestrator.start_run for SSE + reattach).

So all status bookkeeping lives here, not in the caller: we stamp
`last_status='running'` + `last_run_at=now` up front (so a concurrent
scheduler tick won't double-fire), then `ok`/`error` at the end. Output
lands in the artifact store under the scenario id namespace, which doubles
as the scenario's run history (`artifacts.list_artifacts(user_id, scn.id)`).
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncGenerator

from finengine.store import artifacts as _art
from finengine.skills.scenarios import store

log = logging.getLogger(__name__)


async def run_scenario(scn: store.Scenario) -> AsyncGenerator[dict[str, Any], None]:
    store.set_status(scn.id, last_status="running", last_run_at=time.time())
    yield {"type": "started", "kind": scn.kind, "name": scn.name}

    try:
        meta = None
        if scn.kind == "ir_calendar":
            gen = _run_ir_calendar(scn)
        elif scn.kind == "ir_daily":
            gen = _run_ir_daily(scn)
        elif scn.kind == "agent_prompt":
            gen = _run_agent_prompt(scn)
        elif scn.kind == "aifa_monitor":
            gen = _run_aifa_monitor(scn)
        else:
            raise ValueError(f"unknown scenario kind: {scn.kind!r}")
        # Every kind is a generator yielding progress/chat events + a final
        # {"type":"artifact_meta","meta":...} sentinel (consumed here).
        async for ev in gen:
            if ev.get("type") == "artifact_meta":
                meta = ev["meta"]
            else:
                yield ev

        if meta is None:
            # Ran fine but produced nothing to save (e.g. empty calendar).
            store.set_status(scn.id, last_status="ok", last_run_at=time.time())
            yield {"type": "progress", "text": "Готово, но данных для сохранения нет."}
            yield {"type": "done", "run_id": ""}
            return

        store.set_status(
            scn.id, last_status="ok", last_run_at=time.time(), last_run_id=meta.run_id,
        )
        yield {"type": "artifact", "run_id": meta.run_id, "kind": meta.kind}
        yield {"type": "done", "run_id": meta.run_id}
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        log.exception("scenario run failed id=%s: %s", scn.id, msg)
        store.set_status(scn.id, last_status="error", last_error=msg)
        yield {"type": "error", "message": msg}


async def _run_ir_calendar(scn: store.Scenario) -> AsyncGenerator[dict, None]:
    from finengine.skills import ir_calendar

    events: list[dict] = []
    last_done: dict = {}
    # #87: окно периода из params сценария (фронт/чат-тул); пусто → дефолты 14/14.
    _p = scn.params_dict
    async for ev in ir_calendar.build_calendar_stream(
        back_days=_p.get("back_days"), fwd_days=_p.get("fwd_days"),
        instructions=(scn.instructions or "").strip(),
    ):
        t = ev.get("type")
        if t == "events":
            events.extend(ev.get("events", []))
            yield {"type": "progress", "text": f"Собрано событий: {len(events)}…"}
        elif t == "source_done":
            yield {"type": "progress", "text": f"Источник {ev.get('source')}: +{ev.get('events', 0)}"}
        elif t == "started":
            yield {"type": "progress", "text": f"Обхожу {ev.get('companies', 0)} компаний…"}
        elif t == "done":
            last_done = ev
        elif t == "error":
            yield {"type": "progress", "text": f"⚠️ {ev.get('message', '')}"}

    # #95: ручка групп событий — показываем только выбранные (сбор общий).
    # Отсутствующий ключ = группа включена. Группы: reporting/investor_day/other.
    _eg = _p.get("event_groups")
    if isinstance(_eg, dict) and events:
        before = len(events)
        events = [e for e in events if _eg.get(e.get("group", "other"), True)]
        hidden = before - len(events)
        if hidden:
            from finengine.skills.ir_calendar import EVENT_GROUP_LABELS
            off = [EVENT_GROUP_LABELS.get(g, g) for g, on in _eg.items() if not on]
            yield {"type": "progress",
                   "text": f"Группы скрыты ({', '.join(off)}): −{hidden} событий"}

    if not events:
        yield {"type": "artifact_meta", "meta": None}
        return

    companies = last_done.get("total_companies", 0)
    yield {
        "type": "text_delta",
        "text": (
            f"🗓 Собрано {len(events)} событий по {companies} компаниям "
            "(окно ±14 дней). Полная таблица — в истории прогонов сценария."
        ),
    }
    meta = _art.save_artifact(
        user_id=scn.user_id,
        project_id=scn.id,
        kind="ir_calendar",
        payload={"events": events, "lookahead_days": 14},
        title=f"IR Calendar — {len(events)} событий",
        summary=f"{last_done.get('total_companies', 0)} компаний, окно ±14 дней",
        params={"trigger": "scenario", "scenario_id": scn.id},
        counts={"events": len(events), "companies": last_done.get("total_companies", 0)},
    )
    yield {"type": "artifact_meta", "meta": meta}


async def _run_ir_daily(scn: store.Scenario) -> AsyncGenerator[dict, None]:
    from finengine.skills.news.pipeline import run_daily_news_pipeline

    yield {"type": "progress", "text": "Собираю дайджест…"}
    # #97: ручки Daily — период сбора и группы источников из params сценария.
    _p = scn.params_dict
    try:
        _sh = max(6, min(336, int(_p.get("since_hours") or 48)))
    except (TypeError, ValueError):
        _sh = 48
    _mm = str(_p.get("macro_mode") or "both")
    res = await run_daily_news_pipeline(
        user_id=scn.user_id,
        focus=(scn.instructions or "").strip(),
        since_hours=_sh,
        source_groups=_p.get("source_groups") if isinstance(_p.get("source_groups"), dict) else None,
        macro_mode=_mm if _mm in ("both", "week_only", "full") else "both",
    )
    if res.get("error"):
        raise RuntimeError(str(res["error"]))
    kept = res.get("items_kept", 0)
    md = res.get("markdown", "")
    # #97: пер-источник статистика — видна в прогоне (диагноз «новостей нет»).
    _st = res.get("source_stats") or {}
    if _st.get("per_source"):
        _top = sorted(_st["per_source"].items(), key=lambda x: -x[1])
        _line = ", ".join(f"{n}: {c}" for n, c in _top[:8])
        _fails = _st.get("failed") or {}
        _ftxt = ("; упали: " + ", ".join(f"{n} ({e})" for n, e in list(_fails.items())[:4])) if _fails else ""
        _zeros = [n for n, c in _st["per_source"].items() if c == 0 and n not in _fails]
        _ztxt = ("; пусто: " + ", ".join(_zeros[:6])) if _zeros else ""
        _total = sum(_st["per_source"].values())
        yield {"type": "progress",
               "text": f"Источники ({_st.get('sources_total', 0)}, всего {_total} новостей): {_line}{_ftxt}{_ztxt}"}
    if md:
        # Stream the digest into the run chat so the result is visible inline.
        yield {"type": "text_delta", "text": md}
    meta = _art.save_artifact(
        user_id=scn.user_id,
        project_id=scn.id,
        kind="ir_daily",
        payload={
            "markdown": md,
            "meta": {
                "items_kept": kept,
                "items_raw": res.get("items_raw", 0),
                "cost_usd": res.get("cost_usd", 0.0),
            },
        },
        title="Daily News — дайджест",
        summary=f"{kept} новостей",
        params={"trigger": "scenario", "scenario_id": scn.id},
        counts={"items": kept},
        tokens={
            "in": res.get("tokens_in", 0),
            "out": res.get("tokens_out", 0),
            "cost_usd": res.get("cost_usd", 0.0),
        },
    )
    yield {"type": "artifact_meta", "meta": meta}


async def _run_agent_prompt(scn: store.Scenario) -> AsyncGenerator[dict, None]:
    from finengine.orchestrator.agent import get_agent

    if not scn.instructions.strip():
        raise ValueError("scenario has no instructions")

    agent = get_agent()
    parts: list[str] = []
    yield {"type": "progress", "text": "Агент выполняет задачу…"}
    # Инструкция пользователя = навык автоматизации (модель Cowork). Голое
    # сообщение в чат-агента стирало задачу к чат-паттернам (реальный кейс:
    # «собирай информацию по нейронным сетям» → агент позвал daily_news_digest
    # и вернул рыночный дайджест). Рамка: исполняй РОВНО инструкцию, тулы —
    # строго под задачу, без уточняющих вопросов (отвечать некому).
    task = (
        "## Автоматизация пользователя (запуск по кнопке или расписанию)\n\n"
        f"Название: {scn.name}\n"
        + (f"Описание: {scn.description}\n" if scn.description.strip() else "")
        + "\nВыполни инструкцию ниже как самостоятельную задачу и выдай готовый "
        "результат одним ответом (markdown). Правила:\n"
        "- Инструменты выбирай СТРОГО под задачу. `daily_news_digest` и "
        "`ir_calendar_run` — ТОЛЬКО если инструкция явно просит рыночный "
        "дайджест или IR-календарь; для сбора информации по произвольной теме "
        "используй `tavily_search`/`fetch_url` и собери собственную сводку.\n"
        "- НЕ задавай уточняющих вопросов и не зови `ask_user` — это фоновый "
        "запуск, отвечать некому. Действуй по разумным дефолтам и оговори "
        "допущения в конце.\n"
        "- Результат — законченный, пригодный для сохранения в историю прогонов.\n\n"
        f"## Инструкция\n{scn.instructions}"
    )
    # The scenario's chosen project (if any) gives the agent its document
    # context; artifacts still persist under the scenario id for history.
    async for ev in agent.query_stream(
        task,
        auth_user_id=scn.user_id,
        project_id=scn.project_id or scn.id,
    ):
        t = ev.get("type")
        if t == "text_delta":
            parts.append(ev.get("text", ""))
            yield ev
        elif t in ("thinking", "tool_call", "tool_output", "tool_progress", "sources"):
            # Relay the agent's live activity so the run renders as a chat.
            yield ev
        elif t == "error":
            raise RuntimeError(ev.get("error", "agent error"))
        # session_id / done — run_scenario emits its own done.

    text = "".join(parts).strip()
    if not text:
        yield {"type": "artifact_meta", "meta": None}
        return

    summary = text.splitlines()[0][:80] if text else ""
    meta = _art.save_artifact(
        user_id=scn.user_id,
        project_id=scn.id,
        kind="agent_prompt",
        payload={"markdown": text},
        title=scn.name,
        summary=summary,
        params={"trigger": "scenario", "scenario_id": scn.id, "instructions": scn.instructions},
    )
    yield {"type": "artifact_meta", "meta": meta}


async def _run_aifa_monitor(scn: store.Scenario) -> AsyncGenerator[dict, None]:
    """Scenario adapter: schedule the ordinary AIFA Target/graph runtime."""
    from finengine.contracts import MonitorResult, WatchSubscription
    from finengine.skills.atoms.graph_atoms import build_registry, default_policy
    from finengine.skills.case_runtime import CaseRuntime
    from finengine.skills.watch_subscriptions import run_watch_subscription
    from finengine.store.cases import get_case_store

    raw = scn.params_dict.get("subscription")
    if not raw:
        raise ValueError("aifa_monitor scenario has no WatchSubscription")
    subscription = WatchSubscription.model_validate(raw)
    if subscription.owner_id != str(scn.user_id):
        raise PermissionError("WatchSubscription owner does not match scenario owner")
    policy = default_policy(policy_version=subscription.policy_version)
    runtime = CaseRuntime(store=get_case_store(), registry=build_registry(), policy=policy)
    yield {"type": "progress", "text": "Запускаю обычный граф MONITOR_CHANGES…"}
    planned, result = run_watch_subscription(runtime, subscription)
    if not result.succeeded:
        raise RuntimeError("MONITOR_CHANGES PlanRun failed")
    monitor = next(
        (value for values in result.evidence_by_key.values() for value in values
         if isinstance(value, MonitorResult)),
        None,
    )
    if monitor is None:
        raise RuntimeError("MONITOR_CHANGES succeeded without MonitorResult")
    yield {
        "type": "text_delta",
        "text": (
            f"Наблюдение завершено: новых фактов {len(monitor.diff.new_facts)}, "
            f"изменённых {len(monitor.diff.changed_facts)}, "
            f"исчезнувших {len(monitor.diff.disappeared_facts)}."
        ),
    }
    meta = _art.save_artifact(
        user_id=scn.user_id, project_id=scn.id, kind="aifa_monitor",
        payload=monitor.model_dump(mode="json"), title=scn.name,
        summary=f"Subject diff {monitor.diff.comparability}",
        params={
            "trigger": "scenario", "scenario_id": scn.id,
            "plan_run_id": planned.record.plan_run_id,
            "source_snapshot_ids": monitor.trace.source_snapshot_ids,
            "source_plan_run_ids": monitor.trace.source_plan_run_ids,
        },
    )
    yield {"type": "artifact_meta", "meta": meta}
