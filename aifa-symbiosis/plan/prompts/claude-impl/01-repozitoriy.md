# Реализация · шаг 01 · Единый репозиторий и перенос AIFA

Ты — кодовый агент симбиоза AIFA и ouroboros (Claude Code, Sonnet 5.5, усилие High). Эта сессия —
ровно один шаг плана `aifa-symbiosis/plan/README.md`. Рабочая папка —
`C:\Users\magom\Desktop\ouroboros`. Исходник AIFA — `C:\Users\magom\Desktop\Krab_ext\AIFA-CLAW`,
**только чтение**: в `Krab_ext` ничего не писать и не запускать там git-команд, меняющих состояние.

## Прочитай до работы
1. `aifa-symbiosis/architecture/RULES.draft.md`, часть Б (правила симбиоза; `aifa/CLAUDE.md` ещё нет).
2. `aifa-symbiosis/plan/PRINCIPLES.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — §2, §4, §5 (R2, R3).
4. ADR-0006, ADR-0019, ADR-0020, **ADR-0030** (git-база); `aifa-symbiosis/architecture/ARCHITECTURE.md`
   §3, §8 этап 0.

## Постановка задачи
Создать основу единого репозитория в `Rasiksss/ouroboros_external`: отметку базы ouroboros, ветку
шага от `dev`, перенос кода AIFA в `aifa/` без изменения поведения и базовую линию тестов.

## Описание
- **История AIFA не переносится** (О1, `PROGRESS.md` §5 R3): снимок одним структурным коммитом.
- **Состояние на старте** (проверь, расхождение — стоп и запись): `origin` =
  `https://github.com/Rasiksss/ouroboros_external.git`; ветка `dev`, первый (корневой) коммит
  `17b3f93` — снимок ouroboros `release/1.5.1` @ `ec596862` + `aifa-symbiosis/`; `origin/main` =
  `0e327f1` (старая внешняя копия, с `dev` не связана). Рабочее дерево может содержать
  незакоммиченные правки `aifa-symbiosis/` (документы плана) — они твои для первого коммита.
- **База и ветка** (ADR-0030): тег `base/ouroboros-1.5.1` на `17b3f93` (локально, без push); ветка
  шага `sym/01-repozitoriy` от `dev`. `main` не трогать, в `dev` ничего не сливать в этом шаге.
- **Первый коммит ветки** — незакоммиченные изменения `aifa-symbiosis/` как есть (документы
  архитектуры и плана).
- **Перенос AIFA** из `feature/Claw-v2-1.0` @ `f4a12fc6` (проверь, что HEAD исходника такой; нет —
  стоп и запись):
  - `src/`, `tests/`, `pyproject.toml` → `aifa/finengine/`;
  - `web/` → `aifa/web/` (без `node_modules`, `.next`);
  - `docs/build-plan/34*.md`, `docs/build-plan/34b-sessions/`, `docs/build-plan/01-nerushimye-resheniya.md`,
    `docs/rules/coding_standards.md` → `aifa/finengine/docs/` (та же вложенность);
  - не переносить: корневые заметки AIFA, `data/` (кроме того, без чего тесты не идут — тогда
    в `aifa/finengine/` с той же относительной раскладкой), `deploy/` (будет шаг 03), выгрузки,
    `*.tar*`, `.codex-*`, `__pycache__`, любые `.env*`, ключи, сертификаты.
  - Способ: файлы берутся **из коммита `f4a12fc6`, а не из рабочего дерева** AIFA-CLAW (там есть
    незакоммиченные правки и мусор сессий): `git -C <Krab_ext/AIFA-CLAW> archive f4a12fc6 <пути>`
    во временный каталог скретчпада (команда только читает исходник), оттуда — раскладка в `aifa/`.
  - Один структурный коммит переноса; сообщение: «Перенос AIFA: Baltazar2795/AIFA-CLAW,
    feature/Claw-v2-1.0 @ f4a12fc6 (09.10.2026), без истории» + список перенесённых путей. История
    и хэши, на которые ссылается `34-status.md`, остаются в исходном репозитории.
- **Окружение службы.** `uv lock --project aifa/finengine` → `aifa/finengine/uv.lock`; `uv sync
  --frozen --project aifa/finengine`. `mcp<2` пока остаётся (удаляется в шаге 11).
- **Пути.** Если тесты или код опираются на путь от корня AIFA (`data/…`, `docs/…`), допустима
  только минимальная правка пути, каждая — отдельной строкой в отчёте. Логику не менять.
- **Базовая линия.** Прогон тестов службы (`uv run --project aifa/finengine pytest aifa/finengine/tests -m "not integration and not browser"` — маркеры уточни по `pyproject.toml`) и фронтенда (`npm ci && npm test` в `aifa/web`). Сравни с известной базовой линией AIFA (`PROJECTS_STATE.md` §1.6: на Windows красные по окружению; три теста `test_ingress_v2.py`). Ворота ouroboros `./scripts/check.sh --fast` — один раз, чтобы записать его состояние на этой машине.

## Образ целевого результата
- Тег `base/ouroboros-1.5.1` → `17b3f93`.
- Ветка `sym/01-repozitoriy` от `dev`: коммит `aifa-symbiosis/`, структурный коммит(ы) переноса
  AIFA, коммит `uv.lock` службы, при необходимости — коммит правок путей.
- `git diff --name-only base/ouroboros-1.5.1..HEAD` содержит только `aifa/` и `aifa-symbiosis/`.
- Тесты службы и фронтенда дают ту же картину, что AIFA до переноса; отличия объяснены.
- В `PROGRESS.md` записаны базовые линии трёх наборов (служба, фронтенд, ouroboros).

## Границы
Не делать: проверок архитектуры (шаг 02), Dockerfile и compose (шаг 03), `aifa/CLAUDE.md`
(шаг 02), правок кода AIFA сверх путей, push, ничего на стенде.

## Правила шага
- Структурные коммиты отдельно от любых правок (правила кода AIFA 1.1).
- Ничего секретного: перед коммитом поиск по переносимому дереву (`.env`, `token`, `secret`,
  `password`, `BEGIN … KEY`) — находки не коммитить, записать.
- Ветки — только `sym/NN-*` (ADR-0030); `feature/*` не использовать.
- Push — нет (ни ветки, ни тега); `origin/main` не трогать.

## Проверки готовности
| Что | Чем |
|---|---|
| База отмечена | `git rev-parse base/ouroboros-1.5.1` = `17b3f93aa0c7…` |
| ouroboros не тронут | `git diff --name-only base/ouroboros-1.5.1..HEAD` |
| Ничего не ушло наружу | `git log origin/dev..HEAD` — только коммиты шага; `git ls-remote --tags origin` — тега нет |
| Поведение AIFA не изменилось | базовая линия тестов службы и фронтенда = базовой линии AIFA |
| Снимок равен исходнику | `git diff --no-index` временного каталога из `git archive f4a12fc6` против `aifa/…` — только правки путей из отчёта; сообщение коммита переноса называет исходник |
| Нет секретов | поиск по дереву пуст |

## Конец сессии
По `PRINCIPLES.md` §Д (ворот симбиоза ещё нет — вместо них три прогона из «Базовой линии»).
`PROGRESS.md` §1: шаг 01 — «готово» или «остановлено».
