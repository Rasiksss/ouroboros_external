"""Сквозной тест шага 10: `POST /chat/turns` → ouroboros → `aifa_goal` → конвейер на фикстурном
документе → SSE.

Все звенья настоящие, кроме модели (подставной сервер) и границ конвейера (`pipeline_fixture.py`):
ручка `/chat/turns` и `GET /chat/runs/{id}/events` — по HTTP на настоящем приложении службы;
ouroboros — контейнер
профиля `contract` с навыками `aifa-engine` и `aifa-planning` из этого дерева; плагин ходит в
`tool_api` службы
по сети. Закрывает связность шага 10 (PRINCIPLES.md §Б.3) и показывает ход глазами экрана.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pipeline_fixture as fixture
import pytest
from engine_host import running_engine

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

GOAL_TOOL = "ext_13_r_aifa-engine_aifa_goal"
ANSWER = "Выручка за 2024 год — 987654 млн руб."
READ_TIMEOUT_SEC = 120.0


async def screen_events(base: str, run_id: str) -> list[dict[str, Any]]:
    """SSE `GET /chat/runs/{id}/events` до `completed`; тип события — как его читает экран."""
    seen: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT_SEC)) as client:
        async with client.stream("GET", f"{base}/chat/runs/{run_id}/events") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                envelope = json.loads(line[len("data: ") :])
                seen.append(envelope)
                if envelope["event_type"] == "completed":
                    break
    return seen


def kind_of(envelope: dict[str, Any]) -> str:
    return (
        str(envelope["legacy_event"]["type"])
        if envelope["event_type"] == "legacy"
        else str(envelope["event_type"])
    )


@pytest.fixture
def engine(stand: ContractStand, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with running_engine(stand, tmp_path, monkeypatch) as base:
        yield base


async def start_turn(base: str, message: str = fixture.QUESTION) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{base}/chat/turns",
            json={"message": message, "project_id": fixture.PROJECT, "doc_ids": [fixture.DOC]},
        )
    assert response.status_code == 200, response.text
    return str(response.json()["run_id"])


async def test_a_question_about_the_report_is_answered_from_the_pipeline_results(
    engine: str, stub: StubModelAdmin
) -> None:
    """Экран видит: сессия → вызов инструмента → граф конвейера → итог инструмента → ответ →
    конец."""
    stub.use_scenario(SCENARIOS / "aifa_goal.json")

    events = await screen_events(engine, await start_turn(engine))

    kinds = [kind_of(e) for e in events]
    assert kinds[0] == "session_id"
    assert kinds.index("tool_call") < kinds.index("plan_graph") < kinds.index("node_run")
    assert kinds.index("node_run") < kinds.index("tool_output") < kinds.index("text_delta")
    assert kinds[-3:] == ["text_delta", "text_segment_end", "completed"], kinds
    assert any(k in {"result", "typed_result"} for k in kinds), (
        "результат конвейера доехал до журнала"
    )
    called = [e["legacy_event"] for e in events if kind_of(e) in {"tool_call", "tool_output"}]
    assert [(c["type"], c["name"]) for c in called] == [
        ("tool_call", "financial_pipeline"),
        ("tool_output", "financial_pipeline"),
    ]
    assert called[1]["ok"] is True
    answer = next(e["legacy_event"]["text"] for e in events if kind_of(e) == "text_delta")
    assert answer.startswith(ANSWER)
    assert events[-1]["status"] == "succeeded"


async def test_the_model_saw_the_planning_skill_and_the_table_of_results(
    engine: str, stub: StubModelAdmin
) -> None:
    """Модель получила навык `aifa-planning`, а таблицу результатов — из `tool_api` по сети: числа
    не её."""
    stub.use_scenario(SCENARIOS / "aifa_goal.json")

    await screen_events(engine, await start_turn(engine))

    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    assert [(e["round"], e["rule"]) for e in asked] == [
        (1, "read-the-planning-skill"),
        (2, "goal-first"),
        (3, "answer-by-the-table"),
    ]
    assert stub.unmatched() == []
    table = asked[2]["last_tool_result"]
    assert table.startswith("ГОТОВО\n") and "SALES_REVENUE" in table and "987654" in table
    # Тело навыка ouroboros держит в защищённом разделе системного сообщения, а не в ответе
    # `skill_read`.
    second_round = json.dumps(asked[1]["messages"], ensure_ascii=False)
    assert "Правила корневой задачи" in second_round, "корень прочитал навык до вызова инструмента"
    assert GOAL_TOOL in asked[0]["tools"], (
        "инструмент `aifa_goal` в первом запросе (F4 на живом ouroboros)"
    )
    # В `OUROBOROS_ENABLED_TOOLS` восемь имён, зарегистрированы шесть (шаги 10 и 13):
    # остальные ядро пропускает без ошибки.
    assert sorted(t for t in asked[0]["tools"] if "aifa-engine" in t) == [
        GOAL_TOOL.replace("aifa_goal", name)
        for name in ("aifa_doc", "aifa_finish", "aifa_goal", "aifa_move", "aifa_state", "aifa_wait")
    ]


async def test_the_catalog_of_the_root_names_the_planning_skill_with_its_subtask_exception(
    engine: str, stub: StubModelAdmin
) -> None:
    """ADR-0043 п. 2: описание в каталоге навыков дочитывается до признака «Навык: <id>.» (предел
    260 знаков)."""
    stub.use_scenario(SCENARIOS / "aifa_goal.json")

    await screen_events(engine, await start_turn(engine))

    main = next(e for e in stub.journal() if e["model"] == "stub-main")
    prompt = json.dumps(main["messages"], ensure_ascii=False)
    # Каталог экранирует описания (`html.escape`), поэтому «<id>» в нём — «&lt;id&gt;».
    assert "<id>distribution:aifa-planning</id>" in prompt
    assert "Навык: &lt;id&gt;.», этот навык не читает" in prompt
