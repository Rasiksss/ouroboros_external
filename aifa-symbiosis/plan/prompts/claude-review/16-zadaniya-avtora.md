# Ревью · шаг 16 · Регистрация атома, отчёты и ожидание подзадач

Ты — ревьюер симбиоза AIFA и ouroboros (Claude Code, Opus 5.5, усилие Medium). Чистый контекст:
этот код писала другая сессия. Задача — найти проблемы. Код продукта и тесты не правишь.
Рабочая папка — `C:\Users\magom\Desktop\ouroboros`.

## Вход (читать в этом порядке)
1. `aifa-symbiosis/plan/prompts/claude-impl/16-zadaniya-avtora.md` — **целиком**.
2. `aifa-symbiosis/plan/PRINCIPLES.md`, `aifa-symbiosis/plan/REVIEW_GUIDE.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — запись реализации шага 16.
4. ADR-0007, ADR-0021, ADR-0029, ADR-0040, ADR-0041, ADR-0042; `CONTRACTS.md` §2.3, §3.0, §3.1
   (`aifa_register_atom`, `aifa_report`, `aifa_wait`), §4, §5; `ARCHITECTURE.md` §6.

## Вертикальное ревью
Путь §6 `ARCHITECTURE.md` в контрактном тесте: корень → `schedule_task` (настоящий ouroboros,
фикстурный навык) → `aifa_wait(task_id)` → подзадача: `aifa_state` → `aifa_report` →
`aifa_register_atom` (ошибка → `ОТКАЗ`, затем успех) → разовая комната → регистрация `eph`-атома
(версия, исходник в `Case`) → `aifa_report done` → итог у корня → шаг с атомом → `aifa_finish`.
Плюс: завершение ожидания по кадру итога без `done`; 4-я неудачная регистрация одного имени;
`aifa_finish` и `aifa_goal` из подзадачи; задача чужого хода; сбой станции (не тратит
попытку); автор службы (`atom_author.py`) регистрирует атом через `atom_intake`.

## Горизонтальное ревью
- Одно место проверки/регистрации атома — `atom_intake`; у `atom_author.py` и роута своей копии нет.
- Ни одной ветки по имени навыка или тексту задания подзадачи (ADR-0041, C4.7).
- Привязка и роли: `task_depth`, `root_task_id` — из контекста вызова, не из аргументов.
- Вызовы `LoopSession` одного хода сериализованы: два одновременных `aifa_move`/`aifa_register_atom`
  не портят состояние (тест есть?).
- `subtask_waits`: нет утечки ожиданий после конца хода; отмена хода прерывает ожидание.
- Снимок схем по ADR-0042; бюджет F4.

## Связность тестов
| Граница | Ожидаемый тест |
|---|---|
| корень ↔ подзадача ouroboros ↔ `tool_api` | C4.1 (настоящая подзадача) |
| подзадача ↔ сессия Workstation корня | C4.2 (живой, пропускаемый) |
| `atom_intake` ↔ комната ↔ `Case` ↔ `LoopSession` | сквозной тест шага, C4.3 |
| кадр итога ↔ `turn_gateway` ↔ `subtask_waits` ↔ `aifa_wait` | C4.6 |
| экран | C4.4 (`author_attempt` в журнале) |
| служба без знания навыков | C4.7 |

## Результат
По `REVIEW_GUIDE.md` §6: `reviews/16.md`, `PROGRESS.md` §1 и §3, вердикт.
