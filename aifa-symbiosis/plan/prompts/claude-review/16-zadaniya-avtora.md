# Ревью · шаг 16 · Задания и инструменты автора

Ты — ревьюер симбиоза AIFA и ouroboros (Claude Code, Opus 5.5, усилие Medium). Чистый контекст:
этот код писала другая сессия. Задача — найти проблемы. Код продукта и тесты не правишь.
Рабочая папка — `C:\Users\magom\Desktop\ouroboros`.

## Вход (читать в этом порядке)
1. `aifa-symbiosis/plan/prompts/claude-impl/16-zadaniya-avtora.md` — **целиком**.
2. `aifa-symbiosis/plan/PRINCIPLES.md`, `aifa-symbiosis/plan/REVIEW_GUIDE.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — запись реализации шага 16, §5 (`author_mode`).
4. ADR-0007, ADR-0008, ADR-0022, ADR-0025, ADR-0029; `CONTRACTS.md` §3.1 (инструменты автора), §4, §5;
   `ARCHITECTURE.md` §6.

## Вертикальное ревью
Путь §6 `ARCHITECTURE.md` при `author_mode=subtask` в контрактном тесте: `aifa_move` (атом-код) →
`АВТОР job` → `schedule_task` (настоящий ouroboros) → подзадача: `aifa_author_brief` →
`aifa_author_attempt` → `aifa_create_atom` (ошибка → `ОТКАЗ`, затем успех) → разовая комната →
регистрация `eph`-атома (версия, исходник в `Case`) → `aifa_wait` корня → шаг с атомом →
`aifa_finish`. Плюс отказные: 4-я проверка, срок задания, задание чужого хода, инструмент автора
из корня, инструмент планировщика из подзадачи, сбой станции (не тратит попытку).

## Горизонтальное ревью
- Одно место проверки/регистрации атома для `atom_author.py` и `aifa_create_atom` — без копии.
- Бриф не шире того, что видит автор AIFA сейчас (ADR-0025) — сравни с `render_author_prompt`.
- Роли и привязка: `task_depth`, `root_task_id` — из контекста вызова, не из аргументов.
- Временный `author_mode` помечен и записан.
- Снимок схем с предложенной ADR; бюджет F4 с восемью инструментами.

## Связность тестов
| Граница | Ожидаемый тест |
|---|---|
| корень ↔ подзадача ouroboros ↔ `tool_api` | C4.1 (настоящая подзадача) |
| подзадача ↔ сессия Workstation корня | C4.2 (живой, пропускаемый) |
| `author_jobs` ↔ комната ↔ `Case` ↔ `LoopSession` | сквозной тест шага |
| пределы | C4.3 |
| экран | C4.4 (`author_attempt` в журнале) |

## Результат
По `REVIEW_GUIDE.md` §6: `reviews/16.md`, `PROGRESS.md` §1 и §3, вердикт.
