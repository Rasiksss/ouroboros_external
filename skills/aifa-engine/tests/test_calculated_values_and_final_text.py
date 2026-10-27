"""Правила после чтения Excel из dev-прогона 61b91465: компоненты расчётных показателей, итог по `facts`."""

from aifa_engine_support import SKILL_DIR

SKILLS = SKILL_DIR.parent


def text(relative: str) -> str:
    return " ".join((SKILLS / relative).read_text(encoding="utf-8").split())


def test_author_writes_components_of_a_calculated_metric_into_the_file() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "Компоненты расчётного показателя выводи в файл всегда" in source
    assert "`derived_fact.expression`" in source
    assert "компоненты не приведены" in source


def test_author_returns_facts_as_strings_taken_from_the_same_values_as_the_cells() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "`facts`" in source
    assert "массив строк" in source
    assert "из тех же значений, что и ячейки" in source


def test_author_names_an_operand_that_is_narrower_or_out_of_scale() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "несоразмерна" in source
    assert "не прячь" in source


def test_root_answers_with_numbers_only_from_facts_or_the_artifact_table() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "`payload.facts`" in source
    assert "Ключевые цифры" in source
    checking = text("aifa-planning/references/plans.md")
    assert "сравнение двух чисел — тоже расчёт" in checking
    assert "сверь каждое число" in checking


def test_root_does_not_attach_a_second_copy_of_the_published_file() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "второй, непроверенный экземпляр" in source
    assert "`file_read` на файл из `ПРИНЯТО` пришлёт второй" in source


def test_task_plan_writes_go_one_at_a_time_with_the_version_from_the_last_reply() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "строго по одной" in source
    assert "`stale expected_version`" in source
    assert "`attempt_id` текущей попытки" in source


def test_plan_author_keeps_quotes_inside_json_strings_valid() -> None:
    source = text("aifa-plan-author/SKILL.md")
    assert "нет неэкранированных" in source
    assert "«ёлочках»" in source


def test_root_without_an_accepted_plan_orders_a_new_one_instead_of_executing() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "предложение не принято, плана нет" in source
    assert "не начинай исполнение без принятого плана" in source


def test_root_does_not_read_the_report_and_takes_engine_values_as_truth() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "### Корень не читает отчёт" in source
    assert "— истина: корень их не сверяет" in source
    assert "`aifa_doc` до заказа плана и при приёмке не вызывай" in source


def test_step_acceptance_is_light_and_rests_on_the_step_result() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "Приёмка лёгкая" in source
    assert "индекс артефактов не листай" in source
    assert 'task_plan(action="accept_step"' in source
    assert "Не пиши `step awaiting_review` и `review` отдельно" in source
    assert "`review` с `decision` `revise`/`blocked`" in source
    assert "операнды, определения показателей и ряды не сверяй" in source
    assert "принимай, только прочитав его входы" not in source


def test_plan_author_redoes_only_the_failed_step_and_is_called_when_the_steps_change() -> None:
    source = text("aifa-plan-author/SKILL.md")
    assert "### Пересмотр после неудачного хода" in source
    assert "Упавший ход корень сначала переделывает сам" in source
    assert "Тебя зовут, когда нужно изменить требования, состав ходов или зависимости" in source
    assert "переделай только этот ход, остальные ходы и их id оставь" in source
    assert "Не повторяй способ, на котором ход уже упал" in source


def test_plan_author_handles_a_missing_metric_by_calculating_searching_or_admitting_it() -> None:
    source = text("aifa-plan-author/SKILL.md")
    assert "Есть в «Рассчитываемых»" in source and "шагом расчёта через компоненты" in source
    assert "Строго извлекаемый" in source and "`web.search`" in source
    assert "дату обращения и тот же период, что у отчёта" in source
    assert "Расчёт без недостающего компонента не планируй" in source
    assert "не подменяй результат близким по названию" in source


def test_root_redoes_a_failed_step_itself_for_any_failure_not_only_for_metrics() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "Ход завершился ошибкой" in source
    assert "плана не бросай и хода не обходи" in source
    assert "переделай этот ход сам" in source
    assert "новую попытку того же хода (`_plan` с новым `attempt_id`) другим способом" in source
    assert "Цель и критерии хода прежние, меняется способ" in source
    assert "Тот же вызов с теми же аргументами не повторяй" in source
    assert "итог плана — `partial`" in source
    assert "`references/artifacts.md`" in source


def test_a_planner_is_ordered_only_when_the_redo_changes_requirements_steps_or_dependencies() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "Подзадача планировщика нужна, когда переделка хода меняет требования, состав ходов или зависимости" in source
    assert "После каждой локальной ошибки планировщика не заказывай" in source


def test_the_service_results_and_metric_gap_live_in_their_own_reference() -> None:
    source = text("aifa-planning/references/artifacts.md")
    assert "# Результаты службы, расчёты и показатели" in source
    assert "## Показатель не найден или не посчитан" in source
    assert "даже если остальные части хода получены" in source
    assert "нет компонента формулы запрошенного расчёта" in source
    assert "Это упавший ход плана" in source
    assert "Близким показателем не подменяй" in source
    assert "пометка «в отчёте отсутствует» недостачу не закрывает" in source


def test_the_way_to_redo_a_metric_step_follows_the_status_written_in_the_state() -> None:
    source = text("aifa-planning/references/artifacts.md")
    assert "раздел «Статус показателей» в `aifa_state`" in source
    assert "Извлечение — «НЕ НАЙДЕН», расчёт — «не пробовали»" in source and "`calculate_registered_metric`" in source
    assert "Расчёт — «НЕ РАССЧИТАН: нет значений: …»" in source
    assert "`web.search`" in source and "недостающих значений" in source
    assert "источник, адрес, дата обращения и период" in source
    assert "Если интернет не разрешён или значений не дал — итог `partial`: назови, чего нет" in source
    assert "уже записано в состоянии, тем же способом не повторяй" in source


def test_a_step_is_redone_by_what_the_state_and_the_executor_recorded_not_by_a_guess() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "по записям, а не по догадке" in source
    assert "`aifa_state`" in source and "«Статус показателей»" in source and "итог прошлого хода" in source
    assert "`get_task_result`" in source
    assert "другим способом, который следует из этих записей" in source


def test_moves_no_longer_send_the_root_to_chase_a_missing_metric_itself() -> None:
    source = text("aifa-planning/references/moves.md")
    assert "следующим ходом рассчитай его" not in source
    assert "один раз" in source and "переделай ход новой попыткой (`artifacts.md`)" in source
    assert "без своих расчётов" in source


def test_finalize_does_not_let_the_root_drop_a_requested_metric_before_the_step_is_redone() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "«не найден» у показателя запроса — сначала переделай ход (`references/artifacts.md`)" in source
    assert "`references/artifacts.md` — результаты, расчёты, недостача" in source


def test_the_root_names_the_company_as_in_the_state_not_by_the_file_name() -> None:
    source = text("aifa-planning/references/artifacts.md")
    assert "Компанию называй так, как она названа в `aifa_state`, а не по имени файла" in source


def test_the_root_never_calculates_by_itself_not_in_its_head_and_not_in_code() -> None:
    guide = text("aifa-planning/SKILL.md")
    assert "сам не считай ничего — ни в уме, ни кодом (`references/artifacts.md`)" in guide
    source = text("aifa-planning/references/artifacts.md")
    assert "## Считать самому нельзя" in source
    assert "Корень не считает ничего: ни в уме, ни кодом" in source
    assert "среднее (в том числе взвешенное по дням)" in source
    assert "сравнение двух чисел тоже" in source
    assert "Своё число не подставляй" in source
    plans = text("aifa-planning/references/plans.md")
    assert "Расчёт сам не делай ни в уме, ни кодом: ход с расчётом идёт исполнительным вызовом" in plans


def test_a_registry_formula_is_never_changed_and_never_replaced_by_an_ad_hoc_one() -> None:
    """ADR-0086: прогон 26.10 — чистый долг посчитан ad-hoc формулой без BANK_DEPOSITS, значения разошлись с отчётом."""
    source = text("aifa-planning/references/artifacts.md")
    assert "## Формулы реестра не меняются" in source
    assert "Показатель из «Рассчитываемых» считается только по формуле реестра" in source
    assert "компоненты, знаки и состав не меняй" in source and "не убирай компонент" in source
    assert "`calculate_ad_hoc_metric` — только для показателя, которого нет в реестре" in source
    assert "под другим названием та же формула — тоже реестровая" in source
    assert "Расчёт без компонента формулы, пусть с пометкой «компонент опущен», принимать нельзя" in source
    assert "Автору атома формулу реестра называй в задании дословно" in source


def test_the_ad_hoc_scenarios_are_for_metrics_outside_the_registry_only() -> None:
    source = text("aifa-planning/references/moves.md")
    assert "Для показателя реестра ad-hoc и свою формулу не применяй" in source
    assert "формулу реестра не меняй (`artifacts.md`)" in source


def test_the_planner_does_not_write_its_own_formula_for_a_registry_metric() -> None:
    source = text("aifa-plan-author/SKILL.md")
    assert "Формулу показателя реестра не меняй и своей не пиши" in source
    assert "шаг называет показатель, формулу берёт служба" in source


def test_the_atom_author_never_drops_or_replaces_a_component_of_a_registry_formula() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "формулу показателя реестра не упрощай и не меняй" in source
    assert "бери из `derived_fact.expression` все компоненты и знаки" in source
    assert "не опускай и не заменяй нулём" in source
