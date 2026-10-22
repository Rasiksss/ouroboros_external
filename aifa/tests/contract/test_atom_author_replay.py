"""Реплей шага 17: планировщик выбирает навык автора, подзадача сдаёт атом со второй попытки.

Записанные ответы автора (`scenarios/atom_author_recorded.json`: ошибка кода, затем исправление
причины) идут через живой ouroboros профиля `contract` с настоящими навыками `aifa-planning` и
`aifa-atom-author` из `skills/`. Корень читает свой навык, находит автора в каталоге навыков своего
промпта и пишет задание сам; подзадача читает навык автора и сдаёт код инструментами `aifa-engine`
(`aifa_state`, `aifa_report`, `aifa_register_atom`). Служба навыка не знает; модель подставная, код
атома исполняет `room_stub.py` (Workstation в профиле нет). Закрывает связность шага 17
(PRINCIPLES.md §Б.3): навык, подзадача, инструменты, комната, `Case`, журнал хода;
C4.1, C4.3, C4.4, C4.6.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_chat_turn_end_to_end import kind_of, screen_events, start_turn
from test_subtasks_end_to_end import engine, results_of, until_quiet  # noqa: F401

from stand import SCENARIOS
from stub_model.client import StubModelAdmin

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

AUTHOR_SKILL = "distribution:aifa-atom-author"
PLANNING_SKILL = "distribution:aifa-planning"
QUESTION = "какая выручка за 2024 год? Нужен атом автора."
RECORDED: dict[str, Any] = json.loads(
    (SCENARIOS / "atom_author_recorded.json").read_text(encoding="utf-8")
)
ATOM: dict[str, Any] = RECORDED["atom"]
FIRST, SECOND = RECORDED["replies"]
TOOL = "ext_13_r_aifa-engine_{name}"


def _call(name: str, arguments: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "arguments": arguments, **extra}]}


def _rule(
    rule_id: str, user: str, round_no: int, reply: dict[str, Any], **when: Any
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "when": {"model": "stub-main", "user_contains": user, "round": round_no, **when},
        "reply": reply,
    }


def author_scenario() -> dict[str, Any]:
    """Корень: навык планирования, цель, извлечение, задание автору, ожидание, шаг с атомом.
    Подзадача: навык автора, состояние, отчёт об ошибке, сдача (отказ), исправленная сдача, итог."""
    register = {
        key: ATOM[key] for key in ("name", "purpose", "requires", "output_type", "output_schema")
    }
    sub = f"Навык: {AUTHOR_SKILL}"
    description = (
        f"{sub}. Сдай атом {ATOM['name']}: {ATOM['purpose']}; вход — выручка SALES_REVENUE из "
        f"таблицы артефактов, результат — {ATOM['output_type']} с полем revenue_x2 (RUB_MLN)."
    )
    rules = [
        {"id": "title", "when": {"model": "stub-light"}, "reply": {"text": "Атом автора"}},
        _rule("sub-reads-its-skill", sub, 1, _call("skill_read", {"skill": AUTHOR_SKILL})),
        _rule("sub-reads-the-state", sub, 2, _call(TOOL.format(name="aifa_state"), {})),
        _rule(
            "sub-reports-the-first-run",
            sub,
            3,
            _call(
                TOOL.format(name="aifa_report"),
                {
                    "label": ATOM["name"],
                    "status": FIRST["status"],
                    "note": FIRST["note"],
                    "code": FIRST["code"],
                },
            ),
        ),
        _rule(
            "sub-registers-the-first-code",
            sub,
            4,
            _call(TOOL.format(name="aifa_register_atom"), {**register, "code": FIRST["code"]}),
        ),
        _rule(
            "sub-registers-the-fixed-code",
            sub,
            5,
            _call(TOOL.format(name="aifa_register_atom"), {**register, "code": SECOND["code"]}),
        ),
        _rule(
            "sub-reports-done",
            sub,
            6,
            _call(
                TOOL.format(name="aifa_report"),
                {"label": "итог", "status": "done", "note": f"Атом {ATOM['name']} принят службой."},
            ),
        ),
        _rule("sub-answers", sub, 7, {"text": f"Готово: атом {ATOM['name']} сдан."}),
        _rule(
            "root-reads-planning",
            "Нужен атом автора",
            1,
            _call("skill_read", {"skill": PLANNING_SKILL}),
        ),
        _rule("root-goal", "Нужен атом автора", 2, _call(TOOL.format(name="aifa_open"), {})),
        _rule(
            "root-extracts",
            "Нужен атом автора",
            3,
            _call(
                TOOL.format(name="aifa_move"),
                {
                    "objective": "получить выручку за 2024 год",
                    "reason": "она нужна атому",
                    "goals": [
                        {
                            "ref": "revenue",
                            "scenario": "extract_registered_metric",
                            "concepts": ["SALES_REVENUE"],
                            "period": "p-2024-fy",
                        }
                    ],
                },
            ),
            last_tool_result_starts_with="ГОТОВО",
        ),
        _rule(
            "root-delegates",
            "Нужен атом автора",
            4,
            _call("schedule_task", {"description": description}),
            last_tool_result_starts_with="ГОТОВО",
        ),
        _rule(
            "root-waits",
            "Нужен атом автора",
            5,
            _call(
                TOOL.format(name="aifa_wait"),
                {"wait_sec": 60},
                arguments_from_last_tool_result={"task_id": "queued ([0-9a-f]+):"},
            ),
            last_tool_result_starts_with="Task request queued",
        ),
        _rule(
            "root-finishes",
            "Нужен атом автора",
            6,
            _call(TOOL.format(name="aifa_finish"), {"action": "finalize"}),
            last_tool_result_starts_with="ГОТОВО",
        ),
        _rule(
            "root-answers",
            "Нужен атом автора",
            7,
            {"text": f"Удвоенная выручка посчитана атомом {ATOM['name']}."},
            last_tool_result_starts_with="ПРИНЯТО",
        ),
    ]
    return {"name": "atom_author_replay", "rules": rules}


async def test_the_planner_picks_the_author_skill_and_the_atom_is_accepted_on_the_second_try(
    engine: str,  # noqa: F811
    stub: StubModelAdmin,
) -> None:
    """Корень выбрал навык по каталогу и написал задание сам; подзадача по настоящему навыку
    автора сдала атом со второй попытки после ошибки; корень получил итог и сделал шаг с атомом
    (C4.1, C4.3, C4.4, C4.6)."""
    stub.use_scenario(author_scenario())
    run_id = await start_turn(engine, QUESTION)

    events = await screen_events(engine, run_id)
    await until_quiet(stub)

    # Каталог навыков в промпте корня называет автора и говорит, когда его звать (ADR-0041 п. 5).
    assert stub.saw(f"<id>{AUTHOR_SKILL}</id>")
    assert stub.saw("Нужен атом с кодом, которого нет в каталоге атомов")

    sub = results_of(stub, f"Навык: {AUTHOR_SKILL}")
    root = results_of(stub, "Нужен атом автора")
    # Настоящие навыки дошли до моделей инструментом `skill_read` ouroboros: ответ — квитанция,
    # текст навыка лежит в защищённом разделе активных навыков следующего запроса.
    assert "skill:aifa-atom-author/SKILL.md" in sub[2] and "skill:aifa-planning/SKILL.md" in root[2]
    assert stub.saw("# Автор атомов с кодом") and stub.saw("### Делегирование")
    # P118: правила контракта атома и пределы отчёта дошли до модели подзадачи вместе с навыком
    # автора, а корню навык планирования велит называть в задании только `dyn:*`.
    assert stub.saw("- `name` — целиком `eph.<snake_case>`") and stub.saw(
        "иначе `ОТКАЗ нет_единицы`."
    )
    assert stub.saw("`output_type` (только `dyn:<snake_case>`, не `CalculatedMetric`")
    # C4.1: состояние принято. C4.3: первая сдача — ОТКАЗ с хвостом ошибки, вторая — ГОТОВО.
    assert sub[3].startswith("ГОТОВО"), sub[3]
    assert sub[5].startswith("ОТКАЗ неверные_аргументы") and "ZeroDivisionError" in sub[5], sub[5]
    assert "осталось попыток: 2" in sub[5]
    assert sub[6].startswith(f"ГОТОВО\nАтом {ATOM['name']}@"), sub[6]
    # C4.6: корень получил итог (`note` из `aifa_report done`) и сделал шаг с принятым атомом.
    assert root[6].startswith("ГОТОВО\n## Итог подзадачи") and "принят службой" in root[6], root[6]
    assert root[7].startswith("ПРИНЯТО"), root[7]
    # C4.4: отчёт и две сдачи одного имени нумеруются одним счётчиком в пределах хода.
    attempts = [
        (e["legacy_event"]["move"], e["legacy_event"]["attempt"], e["legacy_event"]["status"])
        for e in events
        if kind_of(e) == "author_attempt"
    ]
    assert attempts == [(1, 1, "error"), (1, 2, "error"), (1, 3, "ok")], attempts
    assert events[-1]["status"] == "succeeded"
    assert stub.unmatched() == []


def no_inputs_scenario() -> dict[str, Any]:
    """Корень открывает цикл и сразу поручает атом, не получив входов; подзадача сдаёт код и
    получает `нет_входов`, заканчивает итогом, корень закрывает цикл."""
    register = {
        key: ATOM[key] for key in ("name", "purpose", "requires", "output_type", "output_schema")
    }
    sub = f"Навык: {AUTHOR_SKILL}"
    description = (
        f"{sub}. Сдай атом {ATOM['name']}: {ATOM['purpose']}; вход — выручка SALES_REVENUE."
    )
    rules = [
        {"id": "title", "when": {"model": "stub-light"}, "reply": {"text": "Заголовок чата"}},
        _rule("sub-reads-its-skill", sub, 1, _call("skill_read", {"skill": AUTHOR_SKILL})),
        _rule(
            "sub-registers-without-inputs",
            sub,
            2,
            _call(TOOL.format(name="aifa_register_atom"), {**register, "code": FIRST["code"]}),
        ),
        _rule(
            "sub-reports-done",
            sub,
            3,
            _call(
                TOOL.format(name="aifa_report"),
                {
                    "label": "итог",
                    "status": "done",
                    "note": "Атом не сдан: нет входов SALES_REVENUE.",
                },
            ),
        ),
        _rule("sub-answers", sub, 4, {"text": "Атом не сдан: нет входов."}),
        _rule(
            "root-reads-planning",
            "Атом без входов",
            1,
            _call("skill_read", {"skill": PLANNING_SKILL}),
        ),
        _rule("root-goal", "Атом без входов", 2, _call(TOOL.format(name="aifa_open"), {})),
        _rule(
            "root-delegates",
            "Атом без входов",
            3,
            _call("schedule_task", {"description": description}),
            last_tool_result_starts_with="ГОТОВО",
        ),
        _rule(
            "root-waits",
            "Атом без входов",
            4,
            _call(
                TOOL.format(name="aifa_wait"),
                {"wait_sec": 60},
                arguments_from_last_tool_result={"task_id": "queued ([0-9a-f]+):"},
            ),
            last_tool_result_starts_with="Task request queued",
        ),
        _rule(
            "root-gives-up",
            "Атом без входов",
            5,
            _call(TOOL.format(name="aifa_finish"), {"action": "give_up"}),
            last_tool_result_starts_with="ГОТОВО",
        ),
        _rule(
            "root-answers",
            "Атом без входов",
            6,
            {"text": "Атом не создан: нет входов."},
            last_tool_result_starts_with="ПРИНЯТО",
        ),
    ]
    return {"name": "atom_author_no_inputs", "rules": rules}


async def test_missing_inputs_refusal_tells_the_subtask_to_finish_and_the_planner_to_fetch(
    engine: str,  # noqa: F811
    stub: StubModelAdmin,
) -> None:
    """P119: отказ `нет_входов` называет обоих — подзадача заканчивает итогом, входы получает
    планировщик ходом `aifa_move`; корень получает итог подзадачи, атом не создан."""
    stub.use_scenario(no_inputs_scenario())
    run_id = await start_turn(engine, QUESTION.replace("Нужен атом автора", "Атом без входов"))

    events = await screen_events(engine, run_id)
    await until_quiet(stub)

    sub = results_of(stub, f"Навык: {AUTHOR_SKILL}")
    root = results_of(stub, "Атом без входов")
    refusal = sub[3]
    assert refusal.startswith("ОТКАЗ нет_входов"), refusal
    assert "закончи работу итогом (aifa_report done)" in refusal
    assert "их получит планировщик ходом aifa_move и поручит атом заново" in refusal
    assert "сдай атом снова" not in refusal
    assert sub[4].startswith("ГОТОВО\nИтог принят"), sub[4]
    assert root[5].startswith("ГОТОВО\n## Итог подзадачи") and "нет входов" in root[5], root[5]
    assert root[6].startswith("ПРИНЯТО"), root[6]
    assert [e for e in events if kind_of(e) == "author_attempt"] == []
    assert events[-1]["status"] == "succeeded"
    assert stub.unmatched() == []
