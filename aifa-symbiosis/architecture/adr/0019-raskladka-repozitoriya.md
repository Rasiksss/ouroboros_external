# ADR-0019. Раскладка репозитория: ouroboros в корне без изменений, AIFA — в `aifa/`

Статус: принято (14.10.2026, сессия архитектуры)

## Контекст
Пути ouroboros зашиты в его инструменты: `pyproject.toml` и `uv.lock` в корне, `scripts/check.sh`,
`scripts/resolve_rules.py`, `.rules/*.md` с `paths:`, `alembic.ini`, навыки только из
`${OUROBOROS_REPO_DIR}/skills`. Пакет называется `ouroboros`, как и репозиторий. Перенос ouroboros
в подкаталог потребовал бы правки этих файлов — то есть ядра и правил (запрещено).

## Решение
```text
/                         ← ouroboros как есть (ouroboros/, new_core/, transport/, …, pyproject.toml, uv.lock)
├── skills/
│   ├── aifa-planning/    ← навык-инструкция планировщика (SKILL.md + references/)
│   ├── aifa-atom-author/ ← навык-инструкция автора атомов
│   └── aifa-engine/      ← навык-расширение: plugin.py — тонкий HTTP-клиент службы
├── aifa/
│   ├── finengine/        ← служба движка: pyproject.toml, uv.lock, src/finengine/, tests/
│   ├── web/              ← фронтенд Next.js (из AIFA-CLAW/web)
│   ├── contracts/        ← JSON Schema контрактов и эталонные кадры (данные, не код)
│   ├── tests/            ← контрактные и архитектурные проверки симбиоза, опорные записи (golden/)
│   ├── deploy/dev/       ← compose dev, Dockerfile трёх образов, слой Workstation, env.example
│   ├── scripts/check.sh  ← ворота симбиоза: проверки ouroboros + службы + симбиоза
│   ├── CLAUDE.md, AGENTS.md ← правила работы симбиоза (черновик — RULES.draft.md)
└── aifa-symbiosis/       ← документация симбиоза: SYMBIOSIS.md, PROJECTS_STATE.md, architecture/
```
Вне `aifa/`, `aifa-symbiosis/` и `skills/aifa-*` ничего не меняется. Единственное допустимое
исключение — записи в корневых `.gitignore`/`.dockerignore`, если без них нельзя; каждое такое
исключение — отдельный ADR.

Обновления корпоративного ouroboros (если понадобятся) приходят слиянием из удалённого `corp` в
нашу основную ветку; база отмечена тегом `upstream/ouroboros-ec596862`. Раз мы не правим их
файлы, слияние бесконфликтно.

## Следствия
- `aifa/finengine` сохраняет раскладку `src/finengine`: импорты `finengine.*` не меняются.
- Документация симбиоза пока в `aifa-symbiosis/` (как велел владелец); перенос в `aifa/docs/` —
  отдельным решением, если понадобится.
- Навыки `skills/aifa-*` подчиняются `.rules/skills.md` ouroboros (правила по путям `skills/**`) и
  проверяются его набором `uv run pytest skills`.
- В пакеты навыков не кладутся `CLAUDE.md`/`AGENTS.md`: `skill_read` читает любой UTF-8 файл пакета,
  а инвариант 1 ouroboros запрещает документы разработки в контексте задачи.

## Чем проверяется
F1 (заморозка ouroboros), F2 (импорты).
