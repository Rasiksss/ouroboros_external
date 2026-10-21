# ADR-0058. Живой dev: GLM-5.3 на всех ролях, извлечение без размышлений, бюджет графа 15 минут

Статус: принято (владелец, 21.10.2026)
Заменяет: ADR-0057 в части настроек извлечения

## Контекст

Владелец поручил: планировщик — GLM-5.3, извлечение — DeepSeek-V4.1 Pro, кодер — GLM-5.2.
DeepSeek-V4.1 Pro в каталоге OpenRouter нет; владелец выбрал `deepseek/deepseek-v4-pro`. Кодер —
подзадача ouroboros по навыку `aifa-atom-author`: у ouroboros одна `OUROBOROS_MODEL` на корень и
подзадачи, отдельная модель подзадачи — правка ядра (F1), владелец её отложил.

Живой прогон «извлеки показатели, рассчитай, оформи в Excel» по ЛУКОЙЛу (41 показатель): `locate`
2–3,3 мин, `read.value` 5,5–6 мин при бюджете графа 300 с — граф обрывался и ход уходил на этап 2.
`read.value` шёл с умолчанием размышлений `high`. После этого владелец решил: извлечение — GLM-5.3,
бюджет графа — 15 минут, размышления у извлечения убрать.

GLM-5.3 в OpenRouter размышления выключить не даёт: `reasoning_effort=none`, `reasoning.effort=none`,
`reasoning.enabled=false` — HTTP 400 «Reasoning is mandatory for this endpoint». Наименьший принимаемый
уровень — `minimal` (0 токенов размышлений на пробе).

## Решение

- Все роли — `z-ai/glm-5.3` через прежний infproxy (ADR-0056/0057); кодер — на модели корня.
- Служба: `FINENGINE_LOCATE_REASONING_EFFORT=minimal`, новая `FINENGINE_READ_VALUE_REASONING_EFFORT=minimal`;
  `llm.openrouter` принимает уровень `minimal`.
- Новая настройка `FINENGINE_GRAPH_MIN_WALL_SECONDS` — нижний предел времени графа стандартного пути
  (0 — бюджеты вида цели как есть; длинные, как у полного анализа, не укорачиваются). На dev — 900.

## Следствия

Меняется `engine.env` живого стенда (резервные копии `~/aifa-sym-engine.env.before-models-*`, права 600)
и образ `engine`. `ouroboros.env` и infproxy не меняются. Примеры окружения и F9 по-прежнему не
синхронизированы с живым стендом (как в ADR-0056/0057). Отдельная модель кодера — открытая проблема.
Качество извлечения без размышлений не сверено по golden.

## Чем проверяется

`tests/atoms/test_extraction_stage_models.py` (уровень `read.value` из настройки),
`tests/test_reasoning_knob_reaches_the_model.py` (`minimal` в запросе),
`tests/integration/test_evidence_gate_before_dispatcher.py` (нижний предел бюджета).
Пробы infproxy: варианты выключения размышлений GLM-5.3 и `minimal`. Фактические переменные engine
после пересоздания; живой прогон.
