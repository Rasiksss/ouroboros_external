# Ревью · шаг 01 · Единый репозиторий и перенос AIFA

Ты — ревьюер симбиоза AIFA и ouroboros (Claude Code, Opus 5.5, усилие Medium). Чистый контекст:
этот код писала другая сессия. Задача — найти проблемы. Код продукта и тесты не правишь.
Рабочая папка — `C:\Users\magom\Desktop\ouroboros`; `Krab_ext` — только чтение.

## Вход (читать в этом порядке)
1. `aifa-symbiosis/plan/prompts/claude-impl/01-repozitoriy.md` — **целиком**: это вход реализации.
2. `aifa-symbiosis/plan/PRINCIPLES.md`, `aifa-symbiosis/plan/REVIEW_GUIDE.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — §2, §5 (R2, R3), запись реализации шага 01 (коммиты, базовые линии).
4. ADR-0006, ADR-0019, ADR-0020, ADR-0030.

## Вертикальное ревью
Путь «исходник AIFA → репозиторий симбиоза»: для выборки файлов (`dynamic_run.py`,
`run_orchestrator.py`, `api/main.py`, `web/src/lib/projects.ts`, `pyproject.toml`) — содержимое
равно исходнику @ `f4a12fc6` (`git -C Krab_ext/AIFA-CLAW show f4a12fc6:<путь>` против файла в
`aifa/`); правки путей — каждая названа в отчёте и минимальна. Файлы взяты из коммита
`f4a12fc6`, а не из рабочего дерева AIFA-CLAW (незакоммиченные правки `CLAUDE.md`, `AGENTS.md`,
`Nedodelano.md`, неотслеживаемые файлы не попали); перенос — один коммит без истории, сообщение
называет исходник (О1, `PROGRESS.md` §5 R3).

## Горизонтальное ревью
- Git-база (ADR-0030): `git rev-parse base/ouroboros-1.5.1` = `17b3f93…`; ветка шага
  `sym/01-repozitoriy` растёт от `dev`; `main` не тронут; тег не запушен (`git ls-remote --tags origin`).
- F1 вручную: `git diff --name-only base/ouroboros-1.5.1..HEAD` — только `aifa/`, `aifa-symbiosis/`.
- Секреты: поиск по `aifa/` (`.env`, `token`, `secret`, `password`, `PRIVATE KEY`, cookies, адреса prod).
- Лишнее: перенесено ли то, что не должно (`data/` сверх нужного, заметки, архивы, `node_modules`,
  `.next`, `__pycache__`); не потеряно ли нужное (тесты ссылаются на отсутствующие файлы?).
- Структурные коммиты отделены от правок; ветки только `sym/*`; push не было (`git status -sb`,
  `git log --branches --not --remotes`, `git log origin/dev..sym/01-repozitoriy`).

## Связность тестов
Шаг не меняет поведение: «связность» здесь — равенство базовых линий. Перезапусти тесты службы и
фронтенда; сравни с записью реализации и с базовой линией AIFA (`PROJECTS_STATE.md` §1.6). Любое
новое падение без объяснения — блокер.

## Особые риски шага
- `uv.lock` службы разрешил другие версии, чем работали в AIFA, — проверь ключевые пакеты
  (`pydantic`, `fastapi`, `httpx`, `openai`, `sqlalchemy`) против того, что стоит в образе AIFA
  (`Krab_ext/AIFA-CLAW/deploy/Dockerfile.backend` и `pyproject.toml`); расхождение мажорных версий —
  «важно».
- Исходник не изменён: `git -C Krab_ext/AIFA-CLAW status` и HEAD — как до сессии (`git archive`
  только читает).

## Результат
По `REVIEW_GUIDE.md` §6: `aifa-symbiosis/plan/reviews/01.md`, `PROGRESS.md` §1 и §3, вердикт.
