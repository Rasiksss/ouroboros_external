"""Тексты навыков о скорости планирования (прогоны на dev 22.10.2026): форма плана, размер, бухгалтерия `task_plan`, каталог."""

from __future__ import annotations

import pathlib
import re

from aifa_engine_support import SKILL_DIR

SKILLS = SKILL_DIR.parent
PLANNING = SKILLS / "aifa-planning"


def read(path: pathlib.Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def plan_author() -> str:
    return read(SKILLS / "aifa-plan-author" / "SKILL.md")


def plans() -> str:
    return read(PLANNING / "references" / "plans.md")


def guide() -> str:
    return read(PLANNING / "SKILL.md")


def test_the_planner_takes_the_schema_before_it_writes_and_adds_no_fields_of_its_own() -> None:
    """Прогон 22.10: план с `moves_budget` и без `schema_version` отклонён, ушло три подзадачи планирования."""
    text = plan_author()

    assert 'До составления плана получи схему `task_plan(action="schema")`' in text
    assert "поля вне схемы не добавляй" in text


def test_a_step_input_that_names_a_step_is_repeated_in_depends_on() -> None:
    """Прогон 22.10: вход шага ссылался на шаг, которого не было в `depends_on` — отказ дважды в двух прогонах."""
    text = plan_author()

    assert 'source="step"' in text
    assert "обязательно повтори в `depends_on`" in text


def test_the_planner_checks_the_proposal_against_a_list_before_returning_it() -> None:
    text = plan_author()
    check = text.split("Перед отправкой проверь", 1)[1].split("Верни полный объект", 1)[0]

    for item in ("только из схемы", '`source="step"` есть в `depends_on`', "объявлены", "`verification_method`"):
        assert item in check, item


def test_the_proposal_is_compact_and_has_no_conditional_branches_as_steps() -> None:
    """Прогон 22.10: план на 25 КБ и 13 шагов с условными ветками писался три минуты."""
    text = plan_author()

    assert "не больше 12 000 знаков" in text
    assert "критерий на шаг один" in text
    assert "число шагов не ограничено" in text
    assert "Ветвление" in text and "не записывай шагами" in text


def test_the_planner_does_not_reread_what_the_root_already_found_in_the_report() -> None:
    assert "не перечитывай документ" in plan_author()


def test_the_root_hands_the_planner_the_facts_and_the_catalog_findings() -> None:
    step = plans().split("2. `schedule_task(", 1)[1].split("3. Дождись", 1)[0]

    assert "названия подходящих атомов из `aifa_atoms` и пробелы" in step


def test_the_root_does_not_write_a_running_step_that_the_execution_call_starts_itself() -> None:
    """Прогон 22.10: 10 вызовов `step running` в первом прогоне, хотя dispatch ставит `running` сам."""
    text = plans()

    assert "`step` со статусом `running` не записывай" in text
    assert "без исполнительного вызова" in text


def test_the_schema_is_taken_once_per_turn() -> None:
    assert "Схему бери один раз за ход" in plans()


def test_result_refs_are_pasted_from_the_answer_and_never_typed() -> None:
    """Прогон 22.10: два `review` подряд отклонены, в хеше `sha256` модель исказила один знак."""
    text = plans()

    # Ссылки на результаты корень больше не набирает: их берёт `accept_step` (ADR-0079).
    assert "приими шаг одной записью" in text
    assert "`accept_step`" in text


def test_every_executor_call_of_the_root_carries_the_plan_binding() -> None:
    assert "обязателен `_plan" in plans()
    assert "в том числе первый" in plans()


def test_the_root_reads_the_catalog_itself_before_it_orders_the_plan() -> None:
    """Прогон 22.10: корень первого прогона не открыл каталог, планировщик тоже; второй открыл — план стал лучше."""
    text = guide()

    assert "Каталог читаешь сам, до заказа плана" in text
    assert "передай планировщику" in text
    assert text.index("Каталог читаешь сам, до заказа плана") < text.index("### Делегирование")


def test_the_guide_stays_within_its_size_limit() -> None:
    assert len(guide()) <= 9_000


def test_no_new_rule_leans_on_development_vocabulary() -> None:
    for text in (plan_author(), plans(), guide()):
        assert not re.search(r"ADR-|CONTRACTS|DynamicRun|PROGRESS", text)


def test_the_root_does_not_study_the_report_before_it_orders_the_plan() -> None:
    """Прогон 15:16–15:36: корень 8 минут изучал отчёт сам (12 вызовов `aifa_doc`), а план так и не заказал."""
    text = guide()

    assert "### Корень не читает отчёт" in text
    assert "`aifa_doc` до заказа плана и при приёмке не вызывай" in text
    assert "Не знаешь, где в отчёте показатель, — не повод читать" in text


def test_the_root_hands_over_only_what_the_state_already_knows() -> None:
    step = plans().split("2. `schedule_task(", 1)[1].split("3. Дождись", 1)[0]

    assert "профиль отчёта и список периодов из `aifa_state`" in step
    assert "что уже найдено" not in step


def test_the_root_does_not_read_the_skill_of_the_planner() -> None:
    assert "Навык `aifa-plan-author` не читай" in guide()


def test_the_plan_binding_is_for_executor_calls_after_the_plan_is_accepted() -> None:
    """Прогоны 22.10: `_plan` на `aifa_open` дважды и на заказе плана — отказ `PLAN_EXECUTION_REFUSED`."""
    text = plans()

    assert "`aifa_open` и заказ плана — без `_plan`" in text
