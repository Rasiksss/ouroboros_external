# tests/golden/intent/

Эталонный набор пользовательских запросов для будущего `intent.parse`
(Срез 1 и далее). Требование — `docs/build-plan/03-metodologiya-cikla-sreza.md`,
раздел C0.1: не менее 100 запросов, покрывающих точечные показатели, полный
анализ, формы, неоднозначности и неизвестные концепты.

## Формат (`queries.json`)

Список объектов:

| Поле                | Тип                                                                               | Обязательное |
|---------------------|------------------------------------------------------------------------------------|---|
| `id`                 | строка, уникальный идентификатор запроса                                          | да |
| `query`              | строка, буквальный текст пользовательского запроса                                 | да |
| `language`           | `"ru"` \| `"en"`                                                                   | да |
| `expected_category`  | одно из 13 значений `IntentKind` (retrofit v9: `full_analysis`, `extract_registered_metric`, `extract_ad_hoc_metric`, `calculate_registered_metric`, `calculate_ad_hoc_metric`, `extract_structure`, `document_qa`, `compare_metrics`, `explain_change`, `validate_report`, `plan_fact_analysis`, `monitor_changes`, `form`) либо `"ambiguous"` \| `"unknown_concept"` (свойство запроса, не тип задачи) | да |
| `notes`              | строка, почему запрос отнесён к этой категории (не обязательно для очевидных случаев) | нет |
| `expected_output_kind` | документальная пометка «это размеченная, не ambiguous/unknown строка» — используется только по факту присутствия ключа (`test_golden_intent_accuracy.py`), само значение кодом не сверяется; для видов без ещё построенного `root_from_goal`-корня называет будущий тип результата из каталога `26-...md`, а не текущий `TargetNode.output_kind` | нет |
| `expected_concepts`  | список ожидаемых кодов `targets[].concept` — проверяется только для `extract_registered_metric`/`calculate_registered_metric` | нет |
| `expected_role`      | ожидаемая роль (`intent_kind="form"`) | нет |

## Статус

Retrofit v9 (`docs/build-plan/RETROFIT-REPORT-v9.md`, R4): каталог `IntentKind`
расширен до 13 значений (`docs/build-plan/26-arhitekturnaya-fiksaciya-v9.md`,
§1) добавлением `monitor_changes` — 13-е значение распознаётся `intent.parse`,
но producer для его корня графа ещё не реализован в этом срезе (как и шесть
других значений из `_NOT_YET_WIRED_KINDS`). Набор доведён до 156 запросов.
Все 15 категорий (13 значений `IntentKind` + `ambiguous`/`unknown_concept`)
покрыты минимум 5 примерами каждая (проверяется `tests/test_golden_intent_set.py`).
Полностью синтетический набор — не требует внешних отчётов, только знания
предметной области.
