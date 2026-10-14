# Автоматические проверки архитектуры симбиоза

Проверки, которые держат границы без участия человека. Здесь — что и как проверяет каждая;
реализация — этапы 0–2 (`ARCHITECTURE.md` §8). Все проверки идут в `aifa/scripts/check.sh`
(ворота симбиоза) и обязаны быть зелёными перед каждым коммитом этапа.

Принципы:
- проверка падает с сообщением, которое называет нарушение и ADR, который оно нарушает;
- исключение из проверки — только записью ADR и строкой в файле исключений рядом с проверкой;
- проверка не зависит от сети, кроме помеченных «живая» (пропускаются без адреса).

| № | Проверка | Что ловит | Как | Где | ADR |
|---|---|---|---|---|---|
| F1 | Заморозка ouroboros | правку ядра, станции, правил, документов ouroboros | `git diff --name-only upstream/ouroboros-ec596862...HEAD`: каждый путь начинается с `aifa/`, `aifa-symbiosis/` или `skills/aifa-`; иначе — путь из файла исключений, у каждого исключения — номер ADR | `aifa/tests/architecture/test_core_freeze.py` | 0019 |
| F2 | Границы импортов | связывание частей кодом вместо контракта | разбор `ast` всех `.py`: `aifa/finengine/**` не импортирует `ouroboros`, `new_core`, `transport`, `supervisor`, `config_registry`, `infra`, `multiuser_config`, `background_memory`; код ouroboros (всё вне `aifa/` и `skills/aifa-*`) не импортирует `finengine`; `skills/aifa-engine/**` импортирует только стандартную библиотеку, `httpx` и `ouroboros.observability.aef_tracing`; `skills/aifa-planning`, `skills/aifa-atom-author` — без кода; служба не импортирует `boto3`/клиент S3 для каталога О | `aifa/tests/architecture/test_imports.py` | 0003, 0012, 0019, 0020 |
| F3 | Нет текста документа в ответах инструментов | утечку страниц, OCR, разметки, локаторов в ouroboros | (а) статически: строки ответов внутреннего API формирует только модуль `ouro_view`; он не импортирует хранилища распознанного текста и разметки; (б) динамически: «канарейка» — уникальная строка, вписанная в распознанный текст и разметку тестового документа вне участков, которые становятся цитатами; полный опорный ход (стандартный путь + цикл + автор) на подставных моделях; канарейки нет ни в одном ответе инструмента, ни в одном кадре WS | `aifa/finengine/tests/architecture/test_no_document_text.py` | 0004, 0025 |
| F4 | Бюджет и снимок описаний инструментов | выпадение инструмента из первого запроса; тихую смену контракта | загрузить `plugin.py` с подставным `PluginAPI`, собрать схемы как `task_graph.py` (`len(json.dumps(schema, ensure_ascii=False))`): сумма ≤ 7000 (цель) и < 8000 (предел); имена, аргументы, обязательные поля, `safety_policy`, `capability_class`, `timeout_sec` равны `aifa/contracts/tools.snapshot.json` | `skills/aifa-engine/tests/test_schema_budget.py` (набор ouroboros) | 0022 |
| F5 | Неизменность событий экрана | поломку экрана при неизменном фронтенде | (а) JSON Schema каждого типа события SSE (снимок из опорной записи) — каждое событие нового хода ей соответствует; новых типов нет, кроме разрешённых ADR; `thinking` допускается отсутствующим; (б) реплей опорных записей: нормализованный поток (без `run_id`, времени, длительностей, стоимости) равен записи; разрешённые различия — список с номерами ADR | `aifa/tests/contract/test_screen_events.py`, `aifa/tests/golden/` | 0005, 0015 |
| F6 | Нет вложений в кадрах | передачу файлов ouroboros в обход каталога-только-службы | статически: в клиенте хода нет ключа `attachment_ids`; во время исполнения клиент хода проверяет кадр перед отправкой и падает на запрещённых ключах (`attachment_ids`, `detached: true`) | `aifa/finengine/tests/architecture/test_ws_frames.py` | 0025 |
| F7 | Фронтенд говорит только со службой | прямые вызовы ouroboros из браузера | в `aifa/web/src` нет путей `/ws`, `/api/chat/`, `/api/files`, `/api/agents`, заголовков `X-Ouroboros-*` | `aifa/tests/architecture/test_frontend_boundary.py` | 0014, 0028 |
| F8 | Каждое правило цикла держит тест | правило, которое держится только промптом | файл `aifa/contracts/loop_rules.yaml`: строка на каждое правило раздела 8 плана 34 и столбца «Правила» `CONTRACTS.md` §3.1, у каждой — идентификатор теста; проверка: у каждой строки тест существует и собран pytest | `aifa/finengine/tests/architecture/test_loop_rules_mapped.py` | 0023 |
| F9 | Конфигурация dev | дрейф настроек ouroboros и развёртывания | разбор `aifa/deploy/dev/*.env.example` и compose: обязательный набор ADR-0027 (значения и списки отключённых инструментов/навыков = снимок); у ouroboros и Workstation нет публикации портов кроме `127.0.0.1`; имя compose-проекта и сервисов без `prod`; нет ссылок на `finengine-prod-*`, `openclaw-prod-gw`, `workstation-svc-prod`, `finengine-caddy-1`, `infproxy` как на цели изменений; адреса баз службы и ouroboros различны | `aifa/tests/architecture/test_dev_config.py` | 0010, 0013, 0016, 0017, 0018, 0027, 0028 |
| F10 | Остатки OpenClaw | недоудалённый старый путь (этап 5) | поиск по `aifa/` без истории: `openclaw`, `mcp_server`, `station-skills`, `station_bridge`, `aifa_gateway`, `/internal/tools/call`; включается флагом после этапа 5 | `aifa/tests/architecture/test_openclaw_removed.py` | 0009 |
| F11 | Замки окружений | общий замок; возврат `mcp` в службу | корневой `uv.lock` совпадает с базой `upstream/...` (кроме слияний ADR); в `aifa/finengine/uv.lock` нет `mcp` (после этапа 2) | `aifa/tests/architecture/test_locks.py` | 0020 |
| F12 | Заголовки | второе место чеканки владельца; вызовы без трассы | (а) строки `X-Ouroboros-Owner-Id`/`X-Ouroboros-Chat-Id` встречаются в коде службы только в модуле клиента ouroboros; (б) плагин на подставном транспорте шлёт `X-Trace-Id` и `X-Agent-Id` на каждом вызове (инвариант 6 ouroboros) | `aifa/tests/architecture/test_owner_headers.py`, `skills/aifa-engine/tests/test_headers.py` | 0014, 0022 |
| F13 | Дисциплина ADR | изменение архитектуры без решения | если в диффе изменены `aifa-symbiosis/architecture/ARCHITECTURE.md`, `CONTRACTS.md`, `FITNESS.md`, `aifa/contracts/*.snapshot.json` — в том же диффе добавлен файл `adr/NNNN-*.md` или изменена строка статуса существующего; у принятой ADR меняется только строка статуса | `aifa/tests/architecture/test_adr_discipline.py` | 0001 |

## Чем это отличается от проверок ouroboros

`./scripts/check.sh` ouroboros проверяет сам ouroboros (формат, линтер, типы, три набора) и
инварианты `docs/INVARIANTS.md`. Наши проверки его не заменяют и не дублируют: F1 гарантирует,
что мы его не трогаем, остальные — границы между ним и AIFA. Ворота симбиоза запускают обе
стороны.

## Порядок появления

| Этап | Проверки |
|---|---|
| 0 | F1, F2, F9, F11 (замок ouroboros), F13 — сразу; это каркас, который должен быть до кода |
| 1 | F5 (а) — схемы событий из опорной записи |
| 2 | F4, F6, F7, F12, F5 (б), F3 (а) |
| 3 | F8, F3 (б) |
| 4 | F3 (б) с автором |
| 5 | F10, F11 (`mcp`) |
