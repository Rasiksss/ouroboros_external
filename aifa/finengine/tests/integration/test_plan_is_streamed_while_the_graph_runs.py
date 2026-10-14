"""Срез 31, П7 — «план виден сразу и обновляется живьём» на уровне настоящего
`FinengineAgent.query_stream`.

Владелец: план справа в UI «формируется только после завершения задачи, вот
нафиг он мне нужен после того как я получил результат». Причина была в
буферизации событий в `agent.py`. Тест держит фейковый `read.value` «на паузе»,
пока потребитель `query_stream` не получил `plan_graph`; при буферизации до
конца хода потребитель его не получит, пауза истечёт по таймауту и тест упадёт.
"""
from __future__ import annotations

import asyncio
import threading

from finengine.orchestrator import agent as ag

from ingress_support import (  # noqa: F401 — gate_env: autouse-фикстура окружения
    DOC, OWNER, Turn, gate_env, patched_build_registry, set_intent,
)


def test_plan_graph_reaches_the_consumer_before_a_slow_node_finishes(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    set_intent(monkeypatch, {
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    locate_exec, real_read_value = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})

    graph_seen = threading.Event()
    verdict: dict[str, bool] = {}

    def slow_read_value(items, ctx):
        # Пока потребитель не увидел граф — «модель думает». Если события
        # буферизуются до конца хода, `graph_seen` не взведётся никогда.
        verdict["graph_was_visible_while_running"] = graph_seen.wait(timeout=8)
        return real_read_value(items, ctx)

    patched_build_registry(monkeypatch, locate_exec, slow_read_value)
    turn = Turn()
    turn.arm(monkeypatch)

    async def _go():
        agent = ag.FinengineAgent()
        order: list[str] = []
        async for ev in agent.query_stream(
            "какая выручка за 2024 год?", session_id="", doc_ids=[DOC],
            auth_user_id=OWNER, owner_id=OWNER, project_id="p-lukoil", active_doc_id="",
        ):
            order.append(ev.get("type", ""))
            if ev.get("type") == "plan_graph":
                graph_seen.set()
        return order

    order = asyncio.run(asyncio.wait_for(_go(), timeout=60))

    assert verdict["graph_was_visible_while_running"] is True, (
        "plan_graph не дошёл до потребителя, пока узел ещё выполнялся — события снова буферизуются"
    )
    assert order.index("plan_graph") < order.index("node_run"), "топология раньше первого статуса узла"
    assert "tool_call" in order and order.index("tool_call") < order.index("plan_graph"), (
        "чип financial_pipeline появляется вместе с планом, а не после результата"
    )
    assert order.index("plan_graph") < order.index("typed_result"), "результат приходит после плана"
