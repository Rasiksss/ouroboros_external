# finengine.mcp_server — нативный MCP-сервер бизнес-тулов finengine

Длительно живущий streamable-http сервис, который отдаёт бизнес-инструменты
finengine (`REGISTRY` из `finengine.llm.tools`) OpenCLAW-гейтвею **нативно** —
вместо того чтобы FA-CLAW-бэкенд инжектил определения тулов инлайном в каждый
сырой вызов `/v1/responses`. Гейтвей регистрирует сервер один раз
(`mcp.servers.finengine`), а вызовы прогоняются через тот же диспетчер
`execute(name, args, ctx)` — сервер **не переизобретает** ни одного тула.

Связано: `ARCHITECTURE_DECISIONS.md` (нативная миграция MCP-тулы + Skill Hub),
память `aifaclaw-native-migration.md`, задача #49. Whitelist-каталог тулов —
`deploy/tools_whitelist.yaml`.

## Что делает
- **Транспорт — streamable-http** (не per-call stdio-спавн): сервис несёт
  embedding-модель + per-user данные, поэтому живёт как процесс, а не поднимается
  на каждый вызов.
- **`list_tools`** → `whitelist ∩ REGISTRY` минус `ask_user` (см. правило ниже).
  Тула отдаётся с её родным JSON-schema (`REGISTRY[name].parameters`) и описанием.
- **`call_tool`** → строит owner-scoping `ToolContext` из заголовков запроса и
  запускает `await execute(name, args, ctx)`. Ошибка тула логируется и
  возвращается как `{"error": ...}` — сессия никогда не падает.
- **ASGI-app** `finengine.mcp_server:app` (Starlette, mount `/mcp`) — для uvicorn.

## Запуск
```bash
# как модуль (self-hosted uvicorn внутри __main__)
python -m finengine.mcp_server

# либо как ASGI-app под внешним uvicorn (тот же результат)
uvicorn finengine.mcp_server:app --host 0.0.0.0 --port 8848
```
Слушает `FINENGINE_MCP_HOST:FINENGINE_MCP_PORT` (дефолт `0.0.0.0:8848`),
mount-путь **`/mcp`**. На dev-VPS отдаётся на docker-bridge `172.17.0.1:8848`
(как `infproxy` :8820 и `workstation-svc` :8081), наружу не публикуется.

Гейтвей регистрирует:
```jsonc
"mcp": { "servers": { "finengine": {
  "url": "http://172.17.0.1:8848/mcp",
  "transport": "streamable-http"
}}}
```

## Переменные окружения
| Переменная | Назначение | Дефолт |
|---|---|---|
| `FINENGINE_MCP_HOST` | интерфейс, на котором слушает сервер | `0.0.0.0` |
| `FINENGINE_MCP_PORT` | порт | `8848` |
| `FINENGINE_MCP_ALLOWED_HOSTS` | CSV разрешённых `Host`/`Origin` для DNS-rebinding-защиты SDK (напр. `172.17.0.1:8848,localhost`). **Пусто = проверка выключена** (dev-дефолт, сервер и так на приватном интерфейсе) | — |
| `FINENGINE_MCP_DEV_USER_ID` | fallback-identity, когда заголовков нет (standalone-проба, локальный smoke) | — |
| `FINENGINE_MCP_DEV_PROJECT_ID` | fallback-project к тому же | — |
| `FINENGINE_TOOLS_WHITELIST_YAML` | путь к whitelist-каталогу (иначе `deploy/tools_whitelist.yaml`); наследуется из `finengine.llm.tools` | — |

> **Про `FINENGINE_MCP_ALLOWED_HOSTS`.** DNS-rebinding-защита MCP SDK по умолчанию
> пропускает только localhost, из-за чего гейтвей через docker-bridge получает
> `Host: 172.17.0.1:8848` → «Invalid Host header». Сервер биндится на приватный
> интерфейс (loopback / docker-bridge), не в интернет, поэтому dev оставляет
> проверку выключенной. Прод-стенд банка выставляет allowlist в хост своего гейтвея.

## Контракт owner-scoping (заголовки x-aifa-*)
Модель — банковская, из Workstation-адаптера: **адаптер инжектит identity, агент
её не задаёт.** Per-call `ToolContext` собирается из заголовков, которые
гейтвей/адаптер кладёт в каждый запрос:

| Заголовок | → `ToolContext` |
|---|---|
| `x-aifa-user-id` | `user_id` |
| `x-aifa-project-id` | `project_id` |
| `x-aifa-session-id` | `session_id` |
| `x-aifa-role` | `role` (роль аналитика из UI) |

Если HTTP-контекста нет (напр. standalone-проба), `user_id`/`project_id` берутся
из `FINENGINE_MCP_DEV_USER_ID` / `FINENGINE_MCP_DEV_PROJECT_ID`. Тулы, читающие
или пишущие user-scoped данные, требуют непустой identity (`require_user_ctx`) —
без него вернётся ошибка авторизации, а не утечка чужих данных.

## Правило экспонирования тулов
Отдаём ровно `whitelist ∩ REGISTRY` минус native-only:

- **whitelist** — `enabled[]` из `deploy/tools_whitelist.yaml` (git-tracked
  каталог, регулятор УЭК-АУА-7/13). Файл отсутствует/битый → fail-open: все
  зарегистрированные тулы (логируется предупреждением).
- **REGISTRY** — набор реализованных `ToolDef` в `finengine.llm.tools`.
- **минус `ask_user`** (`_NATIVE_ONLY`) — это flow-control агентского цикла
  (маппится на нативный clarify/elicit гейтвея), а не бизнес-функция; исполняется
  рантаймом, никогда через MCP. `call_tool("ask_user", …)` возвращает «не
  экспонируется».

Добавить/убрать тул из выдачи = правка `enabled[]` в whitelist-YAML (см. чек-лист
в шапке того файла), без изменений здесь.

## Standalone-проба коннекта (`list_tools`)
Быстрый smoke без гейтвея: поднять сервер и опросить его MCP-клиентом. Заголовки
`x-aifa-*` можно передать, чтобы проверить и owner-scoping (иначе сработает
dev-fallback identity).

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def probe():
    headers = {"x-aifa-user-id": "1", "x-aifa-project-id": "smoke"}
    async with streamablehttp_client("http://127.0.0.1:8848/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            for t in tools.tools:
                print(t.name)

asyncio.run(probe())
```
Ожидаемый вывод — имена тулов из `enabled[]` (напр. `fe_extract_metrics`,
`fe_analyze`, `daily_news_digest`, …), **без** `ask_user`. Пустой список или
отказ коннекта = сервер не поднят / не тот `Host`/порт / зарезал
`FINENGINE_MCP_ALLOWED_HOSTS`.
