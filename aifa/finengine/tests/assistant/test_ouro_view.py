"""`ouro_view`: единственный формирователь ответов инструментов (CONTRACTS.md §3.0, C3.2)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from case_support import CASE, OWNER, put_artifact, rows_of, runtime_in

from finengine.assistant import ouro_view
from finengine.assistant.ouro_view import Refusal
from finengine.contracts import GoalIR
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.dynamic_run import LoopSession, artifacts_table
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target

# Первая строка ответа — ровно один из кодов §3.0 (с аргументом у `ЖДИ` и `ОТКАЗ`).
FIRST_LINE = re.compile(r"^(ГОТОВО|ЖДИ job=[0-9a-f]+|ЛИМИТ|ПРИНЯТО|ОТКАЗ [а-яa-z_]+|СБОЙ_СЛУЖБЫ.*)$")
LIMIT = 12_000


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def opened_loop(runtime: CaseRuntime) -> LoopSession:
    """Цикл, который открыл агент (ADR-0060 п. 1): сообщение не разобрано, части цели не описаны."""
    session = LoopSession(
        runtime=runtime, owner_id=OWNER, case_id=CASE, goal=GoalIR(raw_query="вопрос", intent_kind="custom_analysis"),
        target=Target(output_type="ReportOutput"), inputs=RunInputs(), entry_stage="agent",
    )
    session.open(deterministic_error="")
    return session


def test_every_kind_of_answer_starts_with_one_code_and_fits_the_limit(tmp_path: Path) -> None:
    """C3.2: первая строка — код §3.0; размер ≤ `result_limit`."""
    runtime = runtime_in(tmp_path)
    put_artifact(runtime, "revenue", {"value": 100})
    texts = [
        ouro_view.opened(opened_loop(runtime), limit=LIMIT),
        ouro_view.waiting("a1b2c3d4"),
        ouro_view.refusal(Refusal.FOREIGN_RUN, "вызов не этого хода"),
        ouro_view.service_fault("сбой конвейера"),
        ouro_view.limited("исчерпан лимит ходов"),
    ]

    for text in texts:
        assert FIRST_LINE.match(first_line(text)), text
        assert len(text) <= LIMIT


def test_the_open_answer_is_the_state_of_the_loop_the_agent_plans_from(tmp_path: Path) -> None:
    """ADR-0060 п. 1: `aifa_open` отвечает `ГОТОВО` и состоянием: сообщение не разобрано, частей цели нет."""
    runtime = runtime_in(tmp_path)

    text = ouro_view.opened(opened_loop(runtime), limit=LIMIT)

    assert first_line(text) == "ГОТОВО"
    assert "сообщение служба не разбирала" in text and "частей пока нет" in text
    assert "## Реестр показателей" in text and "## Итог прошлого хода\nЦикл открыт" in text


def test_the_results_table_is_the_one_the_planner_reads(tmp_path: Path) -> None:
    """`render_state` и `ouro_view` берут «Полученные артефакты» из одной функции, не пишут свою."""
    runtime = runtime_in(tmp_path)
    put_artifact(runtime, "revenue", {"value": 100})
    put_artifact(runtime, "margin", {"value": 0.25})

    text = ouro_view.opened(opened_loop(runtime), limit=LIMIT)

    assert "## Полученные артефакты\nid | тип | концепт | период | значение\n" in text
    assert artifacts_table(list(rows_of(runtime))) in text


def test_a_value_is_not_clipped_by_the_view(tmp_path: Path) -> None:
    """ADR-0040: границы текста нет; `ouro_view` значение не режет — только делит ответ по строкам."""
    runtime = runtime_in(tmp_path)
    put_artifact(runtime, "long", {"text": "ж" * 250})

    text = ouro_view.opened(opened_loop(runtime), limit=LIMIT)

    assert "ж" * 250 in text


def test_a_table_longer_than_the_limit_is_cut_by_whole_rows_and_names_the_rest(
    tmp_path: Path,
) -> None:
    """§3.0: ответ ≤ `result_limit`; остаток — с id, которые просить у `aifa_state`."""
    runtime = runtime_in(tmp_path)
    for index in range(60):
        put_artifact(runtime, f"metric{index:02d}", {"text": "я" * 200, "n": index})
    table = artifacts_table(list(rows_of(runtime))).split("\n")
    assert len(table) == 60

    text = ouro_view.opened(opened_loop(runtime), limit=6000)

    assert len(text) <= 6000
    shown = [line for line in table if line in text]
    assert 0 < len(shown) < 60
    assert shown == table[: len(shown)], "строки отдаются подряд и целиком"
    hidden_ids = [line.split(" | ", 1)[0] for line in table[len(shown):]]
    assert f"Показано {len(shown)} из 60" in text
    assert hidden_ids[0] in text, "id первой скрытой строки названы"
    assert "aifa_state" in text


def test_a_refusal_names_its_code_and_the_reason() -> None:
    text = ouro_view.refusal(Refusal.NOT_THE_ROLE, "aifa_open вызывает только корень хода")

    assert text == "ОТКАЗ не_та_роль\naifa_open вызывает только корень хода"


def test_a_refusal_code_outside_the_contract_does_not_exist() -> None:
    """Коды отказа закрыты контрактом §3.0; новый — новая ADR."""
    assert {code.value for code in Refusal} == {
        "чужой_ход", "не_та_роль", "нет_цикла", "цикл_закрыт", "неверные_аргументы",
        "повтор_тупика", "нет_входов", "не_dyn_тип", "нет_единицы", "нет_документа",
        "файл_не_собран", "лимит_попыток", "лимит_атомов", "лимит_отчётов",
        "задание_не_найдено", "подзадача_не_ответила",
    }  # fmt: skip


@pytest.mark.parametrize("job_id", ["a1b2c3d4", "00ff00ff"])
def test_waiting_says_which_tool_to_call_next(job_id: str) -> None:
    text = ouro_view.waiting(job_id)

    assert first_line(text) == f"ЖДИ job={job_id}"
    assert "aifa_wait" in text
