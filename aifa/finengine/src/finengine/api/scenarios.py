"""HTTP API for Scenarios («Сценарии») — user-defined scheduled tasks.

Mounted from `api/main.py`:

    from finengine.api.scenarios import router as scenarios_router
    app.include_router(scenarios_router)

CRUD over the scenario store + manual "run now" (kicks a run_orchestrator
background task of kind "scenario", so it survives client disconnect and is
resumable via the stream endpoint) + run history (the artifact list under
the scenario id namespace).
"""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from finengine.auth.db import User
from finengine.auth.dependencies import get_current_user
from finengine.store import artifacts as _art
from finengine.skills import run_orchestrator
from finengine.skills.scenarios import runner as _runner
from finengine.skills.scenarios import store

log = logging.getLogger(__name__)

router = APIRouter(tags=["scenarios"])


# ─── Pydantic models ────────────────────────────────────────────────────


class ScheduleBody(BaseModel):
    freq: str = Field("manual", description="manual | hourly | daily | weekly")
    at_time: str = Field("09:00", description="HH:MM, Europe/Moscow")
    dows: list[int] = Field(default_factory=list, description="0=Пн … 6=Вс (для weekly)")


class CreateScenarioBody(BaseModel):
    name: str = Field(..., min_length=1)
    kind: str = Field(..., description="ir_calendar | ir_daily | agent_prompt")
    instructions: str = ""
    description: str = ""
    project_id: str = ""
    model: str = ""
    schedule: ScheduleBody = Field(default_factory=ScheduleBody)
    paused: bool = False


class PatchScenarioBody(BaseModel):
    name: Optional[str] = None
    instructions: Optional[str] = None
    description: Optional[str] = None
    project_id: Optional[str] = None
    model: Optional[str] = None
    schedule: Optional[ScheduleBody] = None
    paused: Optional[bool] = None
    # #87: JSON-настройки запуска per-kind (календарь: back_days/fwd_days 1..120;
    # дайджест: filter — фокус-указание). Мержим с существующими (частичный патч).
    params: Optional[dict] = None


# ─── Helpers ──────────────────────────────────────────────────────────────


def _owned(scenario_id: str, user: User) -> store.Scenario:
    scn = store.get(scenario_id)
    if scn is None or (not user.is_admin and scn.user_id != user.id):
        raise HTTPException(status_code=404, detail="Сценарий не найден")
    return scn


# ─── CRUD ──────────────────────────────────────────────────────────────────


@router.get("/scenarios")
def list_scenarios(user: User = Depends(get_current_user)):
    # Seed the two built-in skill scenarios on first access (idempotent).
    store.ensure_defaults(user.id)
    return {"scenarios": [s.to_dict() for s in store.list_for_user(user.id)]}


@router.post("/scenarios")
def create_scenario(body: CreateScenarioBody, user: User = Depends(get_current_user)):
    if body.kind not in store.KINDS:
        raise HTTPException(status_code=400, detail=f"Неизвестный тип: {body.kind}")
    if body.schedule.freq not in store.FREQS:
        raise HTTPException(status_code=400, detail=f"Неизвестная частота: {body.schedule.freq}")
    if body.kind == "agent_prompt" and not body.instructions.strip():
        raise HTTPException(status_code=400, detail="Для своего промпта нужна инструкция")
    scn = store.create(
        user_id=user.id,
        name=body.name,
        kind=body.kind,
        instructions=body.instructions,
        description=body.description,
        project_id=body.project_id,
        model=body.model,
        freq=body.schedule.freq,
        at_time=body.schedule.at_time,
        dows=body.schedule.dows,
        paused=body.paused,
    )
    return scn.to_dict()


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str, user: User = Depends(get_current_user)):
    return _owned(scenario_id, user).to_dict()


@router.patch("/scenarios/{scenario_id}")
async def patch_scenario(
    scenario_id: str, body: PatchScenarioBody, user: User = Depends(get_current_user)
):
    _owned(scenario_id, user)
    fields: dict = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.instructions is not None:
        fields["instructions"] = body.instructions
    if body.description is not None:
        fields["description"] = body.description
    if body.project_id is not None:
        fields["project_id"] = body.project_id
    if body.model is not None:
        fields["model"] = body.model
    if body.paused is not None:
        fields["paused"] = body.paused
    if body.schedule is not None:
        if body.schedule.freq not in store.FREQS:
            raise HTTPException(status_code=400, detail=f"Неизвестная частота: {body.schedule.freq}")
        fields["freq"] = body.schedule.freq
        fields["at_time"] = body.schedule.at_time
        fields["dows"] = body.schedule.dows
    cur = store.get(scenario_id)
    if body.params is not None:
        # #91: валидация по единой схеме (scenarios/schema.py).
        from finengine.skills.scenarios import schema as _schema
        merged = dict(cur.params_dict if cur else {})
        for k, v in body.params.items():
            if v is None:
                merged.pop(k, None)  # null = снять настройку → дефолт
                continue
            norm, err = _schema.validate_param(cur.kind if cur else "", k, v)
            if err:
                raise HTTPException(status_code=400, detail=err)
            merged[k] = norm if norm is not None else v
        fields["params"] = merged
    scn = store.update(scenario_id, **fields)
    # #92: компилятор инструкции — «инструкция никогда не молчит». После
    # сохранения текста переводим его в диф параметров + честный отчёт
    # (params._compiled_note → виден на фронте под полем инструкции).
    if body.instructions is not None and scn is not None:
        from finengine.skills.scenarios import compiler as _compiler
        rep = await _compiler.compile_instructions(scn, scn.instructions)
        new_params = dict(scn.params_dict)
        new_params.update(rep.get("params_diff") or {})
        if rep.get("note"):
            new_params["_compiled_note"] = rep["note"]
        scn = store.update(scenario_id, params=new_params) or scn
    return scn.to_dict() if scn else {}


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(scenario_id: str, user: User = Depends(get_current_user)):
    _owned(scenario_id, user)
    store.delete(scenario_id)
    return {"ok": True}


# ─── Run now + history ──────────────────────────────────────────────────


@router.post("/scenarios/{scenario_id}/run")
async def run_scenario_now(scenario_id: str, user: User = Depends(get_current_user)):
    """Kick a background run. Returns the run_id; subscribe to progress via
    GET /scenarios/{id}/runs/{run_id}/stream (replays from the start).

    MUST be async: start_run() calls asyncio.create_task(), which needs a
    running event loop — a sync endpoint runs in a threadpool with no loop."""
    scn = _owned(scenario_id, user)

    def _make_runner():
        return _runner.run_scenario(scn)

    run_id = run_orchestrator.start_run(
        _make_runner,
        kind="scenario",
        meta={"user_id": user.id, "scenario_id": scenario_id, "kind": scn.kind},
    )
    return {"run_id": run_id}


@router.get("/scenarios/{scenario_id}/runs/{run_id}/stream")
async def stream_scenario_run(
    scenario_id: str, run_id: str, user: User = Depends(get_current_user)
):
    """SSE: live progress of a scenario run (or journal replay if finished)."""
    _owned(scenario_id, user)

    if run_orchestrator.чужой_прогон(run_id, user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_stream() -> AsyncGenerator[bytes, None]:
        async for ev in run_orchestrator.subscribe(run_id, kind="scenario"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/scenarios/{scenario_id}/runs")
def list_scenario_runs(scenario_id: str, user: User = Depends(get_current_user)):
    """Run history = artifacts saved under this scenario's id namespace."""
    _owned(scenario_id, user)
    metas = _art.list_artifacts(user.id, scenario_id)
    return {"runs": [m.to_dict() for m in metas]}


@router.get("/scenarios/{scenario_id}/runs/{run_id}")
def get_scenario_run(
    scenario_id: str, run_id: str, user: User = Depends(get_current_user)
):
    _owned(scenario_id, user)
    try:
        return _art.load_artifact(user.id, scenario_id, run_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── «Создать с AifClaw» — chat wizard (Phase B2) ─────────────────────────

_WIZARD_SYSTEM = """\
Ты — ассистент, который помогает пользователю создать «сценарий» — повторяющуюся
задачу для финансового агента, выполняемую вручную или по расписанию.

Веди КОРОТКИЙ диалог на русском. Задай 1–3 уточняющих вопроса, чтобы понять:
- что именно агент должен делать при каждом запуске (суть задачи);
- как часто запускать: вручную / каждый час / ежедневно / по будням / еженедельно;
- в какое время по МСК (и в какие дни недели, если еженедельно).
Если пользователь сразу описал всё — не задавай лишних вопросов, сразу к черновику.

Когда данных достаточно, в КОНЦЕ ответа выведи РОВНО ОДИН блок:
```scenario-draft
{
  "name": "короткое название",
  "description": "одно предложение — что делает сценарий",
  "instructions": "полная инструкция агенту: что сделать при каждом запуске",
  "freq": "manual|hourly|daily|weekly",
  "at_time": "HH:MM",
  "dows": [0,1,2,3,4]
}
```
Правила черновика:
- freq: "manual" (только вручную), "hourly" (каждый час), "daily" (ежедневно),
  "weekly" (по дням недели). «По будням» = freq "weekly" + dows [0,1,2,3,4].
- at_time — время по Москве "HH:MM" (для daily/weekly; для manual/hourly ставь "09:00").
- dows — числа 0=Пн … 6=Вс, только для weekly, иначе [].
- Перед блоком напиши одно предложение-резюме (например «Готов создать сценарий: …»).

Когда нужно УТОЧНИТЬ детали — не задавай вопрос обычным текстом, а выведи блок
(ровно один; без scenario-draft в этом же ответе):
```scenario-question
{"prompt": "текст вопроса", "options": ["короткий вариант 1", "вариант 2", "вариант 3"]}
```
Дай 3–5 коротких вариантов ответа — пользователь выберет один или впишет свой.
В одном ответе — ЛИБО один scenario-question, ЛИБО один scenario-draft.
"""

_DRAFT_RE = re.compile(r"```scenario-draft\s*(\{.*?\})\s*```", re.DOTALL)
_QUESTION_RE = re.compile(r"```scenario-question\s*(\{.*?\})\s*```", re.DOTALL)


class AssistantTurn(BaseModel):
    role: str
    content: str


class AssistantBody(BaseModel):
    messages: list[AssistantTurn]


def _norm_draft(d: dict) -> dict:
    freq = d.get("freq", "manual")
    if freq not in store.FREQS:
        freq = "manual"
    dows = [int(x) for x in d.get("dows", []) if isinstance(x, (int, float)) and 0 <= int(x) <= 6]
    return {
        "name": (str(d.get("name", "")).strip() or "Сценарий")[:120],
        "description": str(d.get("description", "")).strip()[:300],
        "instructions": str(d.get("instructions", "")).strip(),
        "freq": freq,
        "at_time": str(d.get("at_time", "09:00"))[:5],
        "dows": dows,
    }


def _parse_wizard(text: str) -> tuple[Optional[dict], Optional[dict], str]:
    """Extract a scenario-draft and/or scenario-question block from the reply.
    Returns (draft|None, question|None, reply-without-blocks)."""
    cleaned = text or ""
    draft = None
    md = _DRAFT_RE.search(cleaned)
    if md:
        try:
            draft = _norm_draft(json.loads(md.group(1)))
        except (json.JSONDecodeError, ValueError):
            draft = None
        cleaned = _DRAFT_RE.sub("", cleaned)
    question = None
    mq = _QUESTION_RE.search(cleaned)
    if mq:
        try:
            q = json.loads(mq.group(1))
            opts = [str(o).strip() for o in q.get("options", []) if str(o).strip()][:6]
            prompt = str(q.get("prompt", "")).strip()
            if prompt or opts:
                question = {"prompt": prompt, "options": opts}
        except (json.JSONDecodeError, ValueError):
            question = None
        cleaned = _QUESTION_RE.sub("", cleaned)
    return draft, question, cleaned.strip()


@router.post("/scenarios/assistant")
async def scenario_assistant(body: AssistantBody, user: User = Depends(get_current_user)):
    """One wizard turn: take the conversation so far, return the assistant's
    reply and (when ready) a parsed `draft` for prefilling the create form."""
    from finengine.llm import openrouter

    lines: list[str] = []
    for m in body.messages[-12:]:
        who = "Пользователь" if m.role == "user" else "Ассистент"
        lines.append(f"{who}: {m.content}")
    lines.append("Ассистент:")
    transcript = "\n".join(lines)

    try:
        reply = await openrouter.complete(transcript, system=_WIZARD_SYSTEM)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    draft, question, cleaned = _parse_wizard(reply or "")
    return {"reply": cleaned or (reply or ""), "question": question, "draft": draft}
