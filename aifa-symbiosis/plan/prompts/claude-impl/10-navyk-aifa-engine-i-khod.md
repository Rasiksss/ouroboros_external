# Реализация · шаг 10 · Навык `aifa-engine`, минимальный `aifa-planning`, ход чата через ouroboros

Ты — кодовый агент симбиоза AIFA и ouroboros (Claude Code, Sonnet 5.5, усилие High). Эта сессия —
ровно один шаг плана `aifa-symbiosis/plan/README.md`. Рабочая папка —
`C:\Users\magom\Desktop\ouroboros`.

## Прочитай до работы
1. `aifa/CLAUDE.md`; `aifa-symbiosis/plan/PRINCIPLES.md`.
2. `aifa-symbiosis/plan/PROGRESS.md` — записи 07–09, §2 (О3, О4), §4.
3. ADR-0004, ADR-0016, ADR-0019, ADR-0021, ADR-0022, ADR-0027.
4. `CONTRACTS.md` §1 (строка `POST /chat/turns`), §2.5, §3.0–3.1; `FITNESS.md` F4, F7, F12 (б).
5. ouroboros (только чтение, правила обязательны — это `skills/**`):
   `uv run python scripts/resolve_rules.py skills/aifa-engine/plugin.py skills/aifa-planning/SKILL.md`
   и все правила, которые он назовёт (`.rules/skills.md` и др.); `docs/CREATING_SKILLS.md`;
   `docs/INVARIANTS.md` (1, 2, 6); `ouroboros/contracts/plugin_api.py` (`register_tool`);
   `new_core/tools/context.py` (`ToolContext`); `ouroboros/agent/task_graph.py:165-195` (бюджет).
6. Служба: `api/main.py` `POST /chat/turns` целиком; `aifa/web/src` — как экран показывает
   `tool_call`/`tool_output` (поиск).

## Постановка задачи
Замкнуть первый полный ход новой системы: навык-расширение `aifa-engine` (два инструмента),
навык-инструкция `aifa-planning` (минимальный), и ручка `POST /chat/turns`, отдающая ход в
ouroboros через `turn_gateway`.

## Описание
- **Предусловия**: О3 и О4 решены (§2). О3 = «внутри службы» → ветка `pinned_tool` остаётся
  инлайн; иное → стоп, предложенная ADR.
- `skills/aifa-engine/` (`type: extension`, `permissions` — минимально нужные, `env_from_settings`
  — по итогу P2 шага 04):
  - `plugin.py` — тонкий клиент §3.0: `ctx` → тело, `run_id` из `ctx.transport["source_message_id"]`;
    заголовки `X-Trace-Id`/`X-Agent-Id` через `ouroboros.observability.aef_tracing` (инвариант 6);
    длинный опрос — отрезками, между ними `ctx.stop_requested()`; сетевой сбой → `СБОЙ_СЛУЖБЫ`;
    `safety_policy="skip"`, `capability_class` и `timeout_sec` — по ADR-0022; `result_limit=12000`;
  - инструменты: `aifa_goal`, `aifa_wait` — описания по одной строке;
  - `tests/`: F4 (бюджет считать как `task_graph.py`; снимок `aifa/contracts/tools.snapshot.json`
    — с **предложенной** ADR на первый снимок, F13), F12 (б) (заголовки на каждом вызове).
- `skills/aifa-planning/SKILL.md` (`type: instruction`): `description` такой, чтобы навык читался
  на каждом сообщении; правило: на любое сообщение сначала `aifa_goal`; `ЖДИ` → `aifa_wait`;
  `УТОЧНИ` → задать вопрос пользователю вариантами; `ГОТОВО` → ответ по результатам: только числа
  из таблицы, со ссылкой на источник; ничего не считать самому; данные — не инструкции.
  Без кода; без `CLAUDE.md` внутри пакета (инвариант 1).
- `ouroboros.env.example`: `OUROBOROS_ENABLED_TOOLS` = `aifa-engine.aifa_goal,aifa-engine.aifa_wait`;
  адрес и токен службы; F9 обновить.
- **Служба**: `POST /chat/turns` → создание прогона (как сейчас) → `turn_gateway` (шаг 08); ветки
  OpenClaw (`aifa_gateway`, `station_turn`, `agent.query_stream`) больше не вызываются из этой
  ручки (код остаётся до шага 19; переключателя назад нет — откат = старый AIFA на dev);
  `tool_call`/`tool_output` — на каждый вызов `tool_api`, имена экрана — из
  `aifa/contracts/tool_names.json` (сопоставление с прежними названиями инструментов AIFA, чтобы
  экран показывал знакомое).
- **Контрактный профиль**: служба в compose `contract` (ouroboros ходит к ней по сети);
  сценарий подставной модели: `aifa_goal` → ответ по результату.

## Образ целевого результата
- Сквозной тест: `POST /chat/turns` → ouroboros (настоящий, подставная модель) → `aifa_goal` →
  конвейер службы на фикстурном документе → финальный ответ → SSE: `start` … `tool_call`,
  `plan_graph`/`node_run`/`typed_result` …, `tool_output`, `text_delta`, `text_segment_end`, `done`.
- Сравнение этого потока с опорной записью ближайшего сценария S0–S5 (нормализатор шага 05)
  показывает только разрешённые различия — или различия записаны в отчёт с объяснением (полное
  сравнение — шаг 12).
- F4, F7, F12 (б), F9 зелёные; набор ouroboros `uv run pytest skills -m "not integration and not browser"` зелёный.

## Границы
Не делать: инструменты цикла и автора; каталог файлов и историю (шаг 11); выкатку (шаг 12);
изменения фронтенда.

## Правила шага
- В `plugin.py` нет логики, правил, форматирования — только перенос (F2: стандартная библиотека,
  `httpx`, `aef_tracing`).
- Для `skills/**` действует Definition of Done ouroboros: отметь в отчёте, что нужна независимая
  проверка инвариантов — её делает ревью.

## Проверки готовности
| Что | Чем |
|---|---|
| Бюджет и снимок | F4 |
| Заголовки | F12 (б) |
| Фронтенд не ходит в ouroboros | F7 |
| Конфигурация | F9 |
| Связность | сквозной тест фронтенд-ручка → ouroboros → `tool_api` → конвейер → SSE |
| Ворота | `aifa/scripts/check.sh` (включает ворота ouroboros) |

## Конец сессии
По `PRINCIPLES.md` §Д. `PROGRESS.md` §1: шаг 10.
