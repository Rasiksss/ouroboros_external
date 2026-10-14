# ADR-0020. Два окружения Python и отдельные образы

Статус: принято (14.10.2026, сессия архитектуры)

## Контекст
Python совпадает (3.12). Вместе зависимости не разрешаются из-за одного пакета: ouroboros —
`mcp==2.1.1`, finengine — `mcp>=1.28,<2` (только для MCP-сервера под OpenClaw, ADR-0009). Без него
разрешаются все 607 пакетов, но общий замок связал бы обновления двух продуктов.

## Решение
- **Окружение ouroboros** — корневой `pyproject.toml` + `uv.lock`, без изменений: `uv sync --frozen`.
  Навык `aifa-engine` исполняется в процессе ouroboros и не добавляет зависимостей: только
  стандартная библиотека, `httpx` (уже есть у ouroboros) и публичные помощники трассировки
  ouroboros (`ouroboros.observability.aef_tracing`).
- **Окружение службы** — `aifa/finengine/pyproject.toml` + свой `aifa/finengine/uv.lock`:
  `uv sync --frozen --project aifa/finengine`. `mcp` исключается из зависимостей на этапе 2 (вместе
  с отключением MCP-сервера), до этого — допустим, т. к. окружения раздельны.
- **Окружение проверок симбиоза** (`aifa/tests`) — окружение службы: проверки стандартной
  библиотекой разбирают исходники обоих продуктов (`ast`) и говорят с ouroboros по сети. Проверки,
  которым нужен сам ouroboros (бюджет описаний, загрузка `plugin.py`), лежат в
  `skills/aifa-engine/tests/` и идут набором ouroboros.
- **Образы** (`aifa/deploy/dev/`): `ouroboros` (контекст — корень, `.dockerignore` исключает `aifa/`,
  публичная база `python:3.12-slim`, как в старой копии на dev), `finengine` (контекст —
  `aifa/finengine`), `frontend` (контекст — `aifa/web`), `workstation` (база `workstation-svc:local` +
  слой комнаты, ADR-0007).

## Следствия
- Нет общего `uv workspace`: он потребовал бы правки корневого `pyproject.toml`.
- `aifa/scripts/check.sh` вызывает `./scripts/check.sh` ouroboros как есть, затем ruff/mypy/pytest
  службы и проверки симбиоза.

## Чем проверяется
F11 (замки: в замке службы нет `mcp` после этапа 2; корневой замок совпадает с базой), F2.
