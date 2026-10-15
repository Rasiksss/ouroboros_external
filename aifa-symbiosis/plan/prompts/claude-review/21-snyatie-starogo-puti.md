# Ревью · шаг 21 · Снятие пути через OpenClaw на dev

Ты — ревьюер симбиоза AIFA и ouroboros (Claude Code, Opus 5.5, усилие Medium). Чистый контекст:
эту работу делала другая сессия. Задача — найти проблемы. Ничего не меняешь ни в коде, ни на стенде.
Рабочая папка — `C:\Users\magom\Desktop\ouroboros`.

## Вход (читать в этом порядке)
1. `aifa-symbiosis/plan/prompts/claude-impl/21-snyatie-starogo-puti.md` — **целиком**.
2. `aifa-symbiosis/plan/PRINCIPLES.md` §Г, `aifa-symbiosis/plan/REVIEW_GUIDE.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — §2 (решение владельца о переключении), запись реализации шага 21.
4. `aifa/deploy/dev/README.md`; `aifa-symbiosis/PROJECTS_STATE.md` §3.

## Доступ к стенду
Только чтение: `docker ps`, `docker inspect`, `ss -ltnp`, `curl` к dev-адресу.

## Вертикальное ревью
Путь пользователя: dev-адрес → (способ переключения) → фронтенд `aifa-sym` → служба → ouroboros.
Проверь, что каждое звено — то, что решил владелец, и что `finengine-caddy-1` не изменён
(конфигурация и время старта).

## Горизонтальное ревью
- prod: снимки до/после — без изменений (имена, статус, время старта); `infproxy`, Caddy не тронуты.
- Снято ровно то, что в списке владельца; тома и данные не удалены без команды.
- Откат описан и выполним.
- Документы (`README.md`, `PROJECTS_STATE.md`) соответствуют факту.

## Результат
По `REVIEW_GUIDE.md` §6: `reviews/21.md`, `PROGRESS.md` §1 и §3, вердикт.
