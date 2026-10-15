"""Чип в чате обязан краснеть на провале — УСЛОВИЕ ПРИЁМКИ ВЛАДЕЛЬЦА №2.

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. «Получилось или нет» нигде не выражалось типом:

* в инлайн-петле поле `ok` у события `tool_output` НЕ ЭМИТИЛОСЬ ВООБЩЕ, а
  приёмник читал `ev.get("ok", True)` — чип был зелёным всегда, включая прогон,
  вернувший ноль показателей;
* в гейтвей-пути `ok=False` ставился только при ИСКЛЮЧЕНИИ, а штатный отказ
  («документ не найден», «нет метрик») приезжал обычным json и выглядел успехом.

Спросить статус у гейтвея нельзя: доказано, что леджер ядра метит провал как
`succeeded` — две живые задачи с итогом «Both attempts failed» имеют статус
succeeded. Статус ядра означает «процесс завершился», а не «работа сделана».
Поэтому источник истины ровно один: САМ ТУЛ.

Конверт навешивается на границе хендлера (`_with_outcome`), а не правкой каждого
`return`: у четырёх тулов их тридцать, и пропустить один — вопрос времени.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: снять `_with_outcome` с любого из четырёх тулов —
тесты покраснеют.
"""
from __future__ import annotations

import json

import pytest

from finengine.llm import tools as T


# ── сам конверт ───────────────────────────────────────────────────────────

def test_failure_becomes_failed_with_reason():
    out = T._stamp_outcome(json.dumps({"error": "extraction returned no metrics"}))
    data = json.loads(out)
    assert data["status"] == T.OUTCOME_FAILED
    assert data["reason"] == "extraction returned no metrics"


def test_success_becomes_ok():
    data = json.loads(T._stamp_outcome(json.dumps({"metrics_extracted": 77})))
    assert data["status"] == T.OUTCOME_OK
    assert "reason" not in data


def test_tool_own_status_is_not_overwritten():
    """Тул вправе сказать «частично» — обёртка не должна это затирать."""
    src = json.dumps({"status": "partial", "reason": "нашёл 4 из 6", "metrics": 4})
    data = json.loads(T._stamp_outcome(src))
    assert data["status"] == T.OUTCOME_PARTIAL
    assert data["reason"] == "нашёл 4 из 6"


@pytest.mark.parametrize("payload", ["не json", "[1,2,3]", ""])
def test_non_dict_payload_is_left_alone(payload):
    """Не json и не объект — не наше дело, портить нельзя."""
    assert T._stamp_outcome(payload) == payload


# ── чтение конверта исполнителями ─────────────────────────────────────────

def test_outcome_of_reads_envelope():
    assert T.outcome_of(json.dumps({"status": "failed", "reason": "нет метрик"})) == (
        "failed", "нет метрик")
    assert T.outcome_of(json.dumps({"status": "ok"})) == ("ok", "")


def test_outcome_of_returns_none_without_envelope():
    """None — это «тул не переведён», а НЕ «успех». Разница принципиальная:
    исполнители на None оставляют прежнее поведение, а не красят чип зелёным
    от своего имени."""
    assert T.outcome_of(json.dumps({"metrics": 5})) is None
    assert T.outcome_of("что-то не json") is None
    assert T.outcome_of(json.dumps({"status": "непонятно"})) is None


# ── тулы действительно обёрнуты ───────────────────────────────────────────

@pytest.mark.parametrize("name", T.ENVELOPE_TOOLS)
def test_envelope_tools_are_wrapped(name):
    """Ловит «завели тул, обернуть забыли» — ту же грабку, что с плагином."""
    handler = T.REGISTRY[name].handler
    assert getattr(handler, "__wrapped__", None) is not None, (
        f"{name}: хендлер не обёрнут _with_outcome — исход тула останется "
        f"невыразимым, а чип зелёным"
    )


@pytest.mark.parametrize("name", T.ENVELOPE_TOOLS)
async def test_wrapped_handler_stamps_failure(name, monkeypatch):
    """Живая проверка обёртки: отказ хендлера доезжает как failed + причина."""
    async def failing(args, ctx):  # noqa: ANN001, ARG001
        return json.dumps({"error": "документ не найден"}, ensure_ascii=False)

    wrapped = T._with_outcome(failing)
    status, reason = T.outcome_of(await wrapped({}, None))
    assert status == T.OUTCOME_FAILED
    assert reason == "документ не найден"


# ── исполнитель гейтвей-пути ──────────────────────────────────────────────

async def test_gateway_executor_marks_chip_red_on_typed_failure(monkeypatch):
    """Штатный отказ (без исключения) обязан дать ok=False и причину на экран."""
    import asyncio

    from finengine.api import internal_tools as it
    from finengine.skills import run_orchestrator as ro

    stop = asyncio.Event()

    def _runner():
        async def gen():
            yield {"type": "run_started"}
            await stop.wait()
        return gen()

    run_id = ro.start_run(_runner, kind="chat", meta={"user_id": 1})
    await asyncio.sleep(0)
    try:
        gw = "agent:aifa-station:openresponses:fe-outcome"
        it.register_session(gw, user_id=1, project_id="p", run_id=run_id)

        async def failing(args, ctx):  # noqa: ANN001, ARG001
            return json.dumps({"error": "нет метрик"}, ensure_ascii=False)

        monkeypatch.setitem(T.REGISTRY, "fe_extract_metrics", T.ToolDef(
            name="fe_extract_metrics", description="stub",
            parameters={"type": "object"}, handler=T._with_outcome(failing)))
        monkeypatch.setattr(T, "_WHITELIST", {"fe_extract_metrics"}, raising=False)

        res = await it.tools_call(
            it.ToolCallBody(tool="fe_extract_metrics", args={}, gateway_session=gw),
            authorization="")
        assert res["ok"] is False, "штатный отказ снова выглядит успехом"

        chips = [e for e in ro.get_run(run_id).events if e.get("type") == "tool_output"]
        assert chips and chips[-1]["ok"] is False, "чип остался зелёным на провале"
        assert chips[-1].get("reason") == "нет метрик", "красный чип без причины"
    finally:
        stop.set()


async def test_gateway_executor_keeps_green_on_success(monkeypatch):
    """Отрицательный контроль: успех обязан оставаться зелёным."""
    import asyncio

    from finengine.api import internal_tools as it
    from finengine.skills import run_orchestrator as ro

    stop = asyncio.Event()

    def _runner():
        async def gen():
            yield {"type": "run_started"}
            await stop.wait()
        return gen()

    run_id = ro.start_run(_runner, kind="chat", meta={"user_id": 1})
    await asyncio.sleep(0)
    try:
        gw = "agent:aifa-station:openresponses:fe-outcome-ok"
        it.register_session(gw, user_id=1, project_id="p", run_id=run_id)

        async def good(args, ctx):  # noqa: ANN001, ARG001
            return json.dumps({"metrics_extracted": 77}, ensure_ascii=False)

        monkeypatch.setitem(T.REGISTRY, "fe_extract_metrics", T.ToolDef(
            name="fe_extract_metrics", description="stub",
            parameters={"type": "object"}, handler=T._with_outcome(good)))
        monkeypatch.setattr(T, "_WHITELIST", {"fe_extract_metrics"}, raising=False)

        res = await it.tools_call(
            it.ToolCallBody(tool="fe_extract_metrics", args={}, gateway_session=gw),
            authorization="")
        assert res["ok"] is True
        chips = [e for e in ro.get_run(run_id).events if e.get("type") == "tool_output"]
        assert chips[-1]["ok"] is True
        assert "reason" not in chips[-1]
    finally:
        stop.set()
