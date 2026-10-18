"""`ouro_view`: единственный формирователь ответов инструментов (CONTRACTS.md §3.0, C3.2)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from case_support import put_artifact, rows_of, runtime_in

from finengine.assistant import ouro_view
from finengine.assistant.goal_result import GoalResult, Verdict
from finengine.assistant.ouro_view import Refusal
from finengine.skills.spec.dynamic_run import artifacts_table

# Первая строка ответа — ровно один из кодов §3.0 (с аргументом у `ЖДИ` и `ОТКАЗ`).
FIRST_LINE = re.compile(
    r"^(ГОТОВО|ЖДИ job=[0-9a-f]+|НУЖЕН_ЦИКЛ|УТОЧНИ|ЛИМИТ|ПРИНЯТО|ОТКАЗ [а-яa-z_]+|СБОЙ_СЛУЖБЫ.*)$"
)
LIMIT = 12_000


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def test_every_kind_of_answer_starts_with_one_code_and_fits_the_limit(tmp_path: Path) -> None:
    """C3.2: первая строка — код §3.0; размер ≤ `result_limit`."""
    runtime = runtime_in(tmp_path)
    put_artifact(runtime, "revenue", {"value": 100})
    rows = rows_of(runtime)
    texts = [
        ouro_view.goal(GoalResult(Verdict.ANSWERED, "готово", rows), limit=LIMIT),
        ouro_view.goal(GoalResult(Verdict.CLARIFY, "какой период?"), limit=LIMIT),
        ouro_view.goal(GoalResult(Verdict.NO_CALCULATION, "вопрос без расчёта"), limit=LIMIT),
        ouro_view.goal(GoalResult(Verdict.NO_DOCUMENT, "документа нет"), limit=LIMIT),
        ouro_view.waiting("a1b2c3d4"),
        ouro_view.refusal(Refusal.FOREIGN_RUN, "вызов не этого хода"),
        ouro_view.service_fault("сбой конвейера"),
    ]

    for text in texts:
        assert FIRST_LINE.match(first_line(text)), text
        assert len(text) <= LIMIT


def test_the_answer_code_follows_the_verdict() -> None:
    answered = ouro_view.goal(GoalResult(Verdict.ANSWERED, "итог"), limit=LIMIT)
    clarify = ouro_view.goal(GoalResult(Verdict.CLARIFY, "какой период?"), limit=LIMIT)
    nothing = ouro_view.goal(GoalResult(Verdict.NO_DOCUMENT, "нет"), limit=LIMIT)

    assert first_line(answered) == "ГОТОВО"
    assert first_line(clarify) == "УТОЧНИ" and "какой период?" in clarify
    assert first_line(nothing) == "ОТКАЗ нет_документа"


def test_the_metric_missing_from_the_report_is_a_result_not_a_refusal() -> None:
    """§3.1: «показателя нет в отчёте» — результат (`ГОТОВО` с этим итогом), а не цикл."""
    text = ouro_view.goal(
        GoalResult(Verdict.ANSWERED, "Показатель EBITDA не найден в отчёте."), limit=LIMIT
    )

    assert first_line(text) == "ГОТОВО"
    assert "Показатель EBITDA не найден в отчёте." in text


def test_the_results_table_is_the_one_the_planner_reads(tmp_path: Path) -> None:
    """`render_state` и `ouro_view` берут «Полученные артефакты» из одной функции, не пишут свою."""
    runtime = runtime_in(tmp_path)
    put_artifact(runtime, "revenue", {"value": 100})
    put_artifact(runtime, "margin", {"value": 0.25})
    rows = rows_of(runtime)

    text = ouro_view.goal(GoalResult(Verdict.ANSWERED, "готово", rows), limit=LIMIT)

    assert "## Полученные артефакты\nid | тип | концепт | период | значение\n" in text
    assert artifacts_table(list(rows)) in text


def test_a_value_is_not_clipped_by_the_view(tmp_path: Path) -> None:
    """ADR-0040: границы текста нет; `ouro_view` значение не режет — только делит ответ по строкам."""
    runtime = runtime_in(tmp_path)
    put_artifact(runtime, "long", {"text": "ж" * 250})
    rows = rows_of(runtime)

    text = ouro_view.goal(GoalResult(Verdict.ANSWERED, "готово", rows), limit=LIMIT)

    assert "ж" * 250 in text


def test_a_table_longer_than_the_limit_is_cut_by_whole_rows_and_names_the_rest(
    tmp_path: Path,
) -> None:
    """§3.0: ответ ≤ `result_limit`; остаток — с id, которые просить у `aifa_state`."""
    runtime = runtime_in(tmp_path)
    for index in range(60):
        put_artifact(runtime, f"metric{index:02d}", {"text": "я" * 200, "n": index})
    rows = rows_of(runtime)
    table = artifacts_table(list(rows)).split("\n")
    assert len(table) == 60

    text = ouro_view.goal(GoalResult(Verdict.ANSWERED, "готово", rows), limit=4000)

    assert len(text) <= 4000
    shown = [line for line in table if line in text]
    assert 0 < len(shown) < 60
    assert shown == table[: len(shown)], "строки отдаются подряд и целиком"
    hidden = table[len(shown) :]
    hidden_ids = [line.split(" | ", 1)[0] for line in hidden]
    assert f"Показано {len(shown)} из 60" in text
    assert hidden_ids[0] in text, "id первой скрытой строки названы"
    assert "aifa_state" in text


def test_an_endless_note_does_not_push_the_answer_over_the_limit(tmp_path: Path) -> None:
    runtime = runtime_in(tmp_path)
    put_artifact(runtime, "revenue", {"value": 100})

    text = ouro_view.goal(GoalResult(Verdict.ANSWERED, "я" * 50_000, rows_of(runtime)), limit=4000)

    assert len(text) <= 4000
    assert "dyn:revenue" in text, "таблица не вытеснена итогом"


def test_a_refusal_names_its_code_and_the_reason() -> None:
    text = ouro_view.refusal(Refusal.NOT_THE_ROLE, "aifa_goal вызывает только корень хода")

    assert text == "ОТКАЗ не_та_роль\naifa_goal вызывает только корень хода"


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
