# Ревью · шаг 10 · Навык `aifa-engine`, минимальный `aifa-planning`, ход чата через ouroboros

Ты — ревьюер симбиоза AIFA и ouroboros (Claude Code, Opus 5.5, усилие Medium). Чистый контекст:
этот код писала другая сессия. Задача — найти проблемы. Код продукта и тесты не правишь.
Рабочая папка — `C:\Users\magom\Desktop\ouroboros`.

Этот шаг меняет `skills/**` ouroboros: ревью — ещё и независимая проверка Definition of Done
ouroboros (инварианты, правила `.rules/`).

## Вход (читать в этом порядке)
1. `aifa-symbiosis/plan/prompts/claude-impl/10-navyk-aifa-engine-i-khod.md` — **целиком**.
2. `aifa-symbiosis/plan/PRINCIPLES.md`, `aifa-symbiosis/plan/REVIEW_GUIDE.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — §2 (О3, О4), запись реализации шага 10.
4. ADR-0004, ADR-0016, ADR-0019, ADR-0021, ADR-0022, ADR-0027; `CONTRACTS.md` §1, §2.5, §3;
   `FITNESS.md` F4, F7, F9, F12.
5. ouroboros: `docs/INVARIANTS.md`; `uv run python scripts/resolve_rules.py <изменённые файлы skills/>`
   и названные правила; `docs/DEVELOPMENT.md` §Coding Rules; `docs/CREATING_SKILLS.md`.

## Вертикальное ревью
Главный путь шага, целиком: `POST /chat/turns` → прогон → `turn_gateway` → WS ouroboros → задача
корня → `skill_read aifa-planning` → вызов `aifa-engine.aifa_goal` → `plugin.py` (контекст, `run_id`
из `transport`, заголовки) → `tool_api` → конвейер → события (`tool_call`, `plan_graph` …,
`tool_output`) → `ouro_view` → модель → финал → `text_delta`/`text_segment_end`/`done` → SSE.
Подними профиль `contract` и прогони сквозной тест сам; сравни поток с опорной записью ближайшего
сценария нормализатором.

## Горизонтальное ревью
- `plugin.py`: только перенос; импорты — стандартная библиотека, `httpx`, `aef_tracing` (F2);
  `safety_policy`, `capability_class`, `timeout_sec`, `result_limit` по ADR-0022; длинный опрос с
  проверкой `ctx.stop_requested()`; сетевой сбой → `СБОЙ_СЛУЖБЫ`.
- Инварианты ouroboros: 1 (в пакетах навыков нет документов разработки), 2 (нет инструментов
  самоизменения), 6 (`X-Trace-Id`/`X-Agent-Id` на каждом вызове), 7 (идентичность не берётся из
  аргументов/полезной нагрузки).
- `SKILL.md`: `description` заставляет читать навык; правила не противоречат ADR-0004/0024; нет
  обещаний, которых не держит код.
- Снимок схем — с **предложенной** ADR (F13); бюджет F4 посчитан как в `task_graph.py`.
- `POST /chat/turns`: старый путь больше не вызывается; ветка `pinned_tool` — по решению О3; поля
  запроса фронтенда принимаются как раньше.
- `tool_names.json`: экран показывает знакомые имена.

## Связность тестов
| Граница | Ожидаемый тест |
|---|---|
| фронтенд-ручка ↔ служба ↔ ouroboros ↔ плагин ↔ `tool_api` ↔ конвейер ↔ SSE | сквозной на настоящем ouroboros (`contract`) |
| плагин ↔ `tool_api` | F12 (б), тест контекста (`run_id` из `transport`) |
| навык ↔ бюджет первого запроса | F4 |
| фронтенд ↔ ouroboros (не должно быть) | F7 |
Наборы: `uv run pytest skills -m "not integration and not browser"` и ворота симбиоза — прогнать.

## Результат
По `REVIEW_GUIDE.md` §6: `reviews/10.md`, `PROGRESS.md` §1 и §3, вердикт (с отдельной строкой
«Definition of Done ouroboros для `skills/**`: выполнено / нет»).
