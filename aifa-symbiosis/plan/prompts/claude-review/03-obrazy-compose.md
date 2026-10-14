# Ревью · шаг 03 · Образы и compose dev

Ты — ревьюер симбиоза AIFA и ouroboros (Claude Code, Opus 5.5, усилие Medium). Чистый контекст:
этот код писала другая сессия. Задача — найти проблемы. Код продукта и тесты не правишь.
Рабочая папка — `C:\Users\magom\Desktop\ouroboros`.

## Вход (читать в этом порядке)
1. `aifa-symbiosis/plan/prompts/claude-impl/03-obrazy-compose.md` — **целиком**.
2. `aifa-symbiosis/plan/PRINCIPLES.md`, `aifa-symbiosis/plan/REVIEW_GUIDE.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — запись реализации шага 03, §4 (P3, P4).
4. ADR-0002, ADR-0007, ADR-0010, ADR-0013, ADR-0016–0018, ADR-0020, ADR-0027, ADR-0028; `FITNESS.md` F9.
5. `docs/INVARIANTS.md` ouroboros (3, 8 — порты, ключ шифрования).

## Вертикальное ревью
Путь «compose → контейнер → процесс»: для `ouroboros` — от `compose.yml` через Dockerfile и env до
команды запуска: какой режим (`server.py`), `OUROBOROS_MULTIUSER=true`, браузерный вход, куда
смотрит на PostgreSQL/S3/Workstation, какие порты опубликованы. То же для `workstation` (база,
слой, отсутствие finengine и моста) и `engine`/`frontend` (профиль `app`).

## Горизонтальное ревью
- Обязательный набор ADR-0027 — значение за значением; каждое `ENV` объяснимо ADR.
- Порты: только `127.0.0.1` у ouroboros, никаких у Workstation и баз (ADR-0028).
- Имена проекта, сервисов, томов, сетей — без `prod`, с префиксом `aifa-sym`.
- Контекст сборки ouroboros не тащит `aifa/` (проверь `docker build` с `--progress=plain` или
  содержимое контекста); исключения — в `ouroboros.Dockerfile.dockerignore`, корень не тронут (F1).
- Секреты: только заглушки; ключ шифрования — инструкция генерации, не значение.
- F9 проверяет то, что написано в `FITNESS.md`, включая «нарушителя».

## Связность тестов
| Граница | Что проверить |
|---|---|
| compose ↔ настройки ouroboros | имена переменных существуют в `ouroboros/storage/settings/schema.py` (опечатка = тихо проигнорированная настройка) |
| compose ↔ настройки службы | имена переменных `engine.env.example` существуют в `Settings` службы |

## Особые риски шага
- Опечатка в имени настройки ouroboros, которая молча не действует (особенно
  `OUROBOROS_DISABLED_TOOLS`, `OUROBOROS_TOOL_CONFIRMATION_ENABLED`).

## Результат
По `REVIEW_GUIDE.md` §6: `reviews/03.md`, `PROGRESS.md` §1 и §3, вердикт.
