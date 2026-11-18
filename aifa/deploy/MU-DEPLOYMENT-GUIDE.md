# GigaAgent MU + AIFA: параметры корпоративного развёртывания

Дата сверки: 05.11.2026. Источник инструкции: локальный `Downloads/html`,
«GigaAgent v6 версия инструкции для установки», Confluence pageId=19378181323.
Проверено по текущим исходникам `gigaagent/`, `aifa/`, `workstation/`.
Это проект конфигурации для первого IFT-стенда, а не подтверждение готовности MU.
Конфигурации приложений этим документом не изменяются. Команды приведены для
выполнения в контуре; при подготовке отчёта они не запускались.

## 1. Состояние реализации и границы проверки

Отдельный MU-runtime теперь использует ExtensionRuntime и общий с server.py
startup-загрузчик доверенных расширений. Зарегистрированы все 9 инструментов
AIFA; при ошибке загрузки startup завершается с очисткой частичной регистрации.
При завершении процесса расширения выгружаются после drain задач и до общих клиентов.

Примеры COMMON, agents, integrations и namespace для IFT подготовлены в
`gigaagent/deploy/mu/`. Пять команд запуска заданы явно, AIFA направлена на
transport:9090. Корневой Dockerfile по умолчанию сохраняет запуск единого сервера;
для MU нужно применить эти отдельные записи Operator.

В MU-примере OUROBOROS_MIGRATE_ON_START=0: один миграционный шаг в Jenkins/Job
должен закончиться до старта рабочих pod. Без флага прежний entrypoint мигрирует
при запуске. Примеры всё ещё требуют реальных bindings/секретов платформы.

Локально проверены регистрация, compose_process, вызовы 9 инструментов через
HTTP route AIFA, контекст двух пользователей, авторизация, отзыв доверия,
повреждённый плагин, unload и режимы entrypoint. Бизнес-обработчик AIFA в этом
HTTP-тесте заменён тестовым; реальная модель и весь распределённый стенд не запускались.
Предыдущие 25 тестов SecMan/Workstation дополняются этими проверками, но это
не подтверждение полной раскатки через Operator и реальные Kafka/PostgreSQL/S3.

## 2. Что выбрать для первого запуска

- Стенд: IFT; один namespace для этого приложения, отдельный от PROM.
- 7 компонентов: 5 компонентов ядра + AIFA + Workstation; фронт/helper не включать.
- Везде по 1 реплике. Это функциональный старт без заявленной высокой доступности.
- У agent-runtime 4 рабочих слота. Значения из примера HTML «4 реплики × 16 слотов»
  пока не брать: они не рассчитаны на нашу нагрузку, модели и бюджет соединений.
- Kafka — межпроцессные сообщения ядра. Кеш настроек отключён:
  OUROBOROS_SETTINGS_CACHE_BACKEND=none. Redis/Radish для этой раскатки не нужен.
- Модели — существующий корпоративный OpenAI-совместимый шлюз. В ядре использовать
  адаптер `cloudru`, в AIFA — её адаптер OpenRouter с внутренним base URL.
  Эти имена адаптеров не означают выход в публичную сеть.
- Дистилляцию памяти, tool confirmation, SberChat/Dialogs, A2A, MCP оставить
  выключенными по текущему AIFA-профилю; не включать интеграции из чужого примера.
- Workstation: временные сессии, IS_CREATE_SESSION_SECRET=true,
  WORKSPACE_STORE_ENABLED=false. Существующая поддержка S3-workspace не означает,
  что клиенты нашего агента уже передают durable scope.

### Компоненты, команды и начальные лимиты

В AGENT_CUSTOM_ARGS для пяти компонентов ядра:
`["/opt/venv/bin/python", "-m", "<модуль>"]`.
Для AIFA и Workstation оставить их штатные entrypoint.

| AGENT_NAME | Модуль / запуск | Исходники в COMMON | Порт | Реплики | Requests CPU/RAM | Limits CPU/RAM |
|---|---|---|---|---|---|---|
| gigaagent | multiuser_config.agent_runtime.run | gigaagent/ | 9090, probes | 1 | 500m / 1Gi | 1000m / 2Gi |
| transport | multiuser_config.transport.browser | gigaagent/ | 9090 | 1 | 250m / 512Mi | 500m / 1Gi |
| supervisor | multiuser_config.supervisor | gigaagent/ | 9090 | 1 | 250m / 512Mi | 500m / 1Gi |
| memory | multiuser_config.background_memory.run | gigaagent/ | 9090, probes | 1 | 100m / 256Mi | 500m / 512Mi |
| config-registry | multiuser_config.config_registry | gigaagent/ | 9090 | 1 | 100m / 256Mi | 500m / 512Mi |
| aifa | штатный entrypoint, finengine.api.main:app | aifa/ | 8080 | 1 | 500m / 1Gi | 1000m / 2Gi |
| workstation | штатный entrypoint, python app.py | workstation/ | 8080 | 1 | 400m / 768Mi | 800m / 1500Mi |

Это предлагаемый стартовый бюджет, не измеренная ёмкость. Sidecar/Vault/mesh
требуют отдельного запаса в квоте namespace. Порты 9090 у разных pod допустимы;
INNERPORT интеграций, напротив, нужно разводить в соответствии с шаблоном mesh.

Probes ядра: `/ready` и `/live` на 9090. В agents/stand.yaml явно объявить этот
порт также в ADDITIONAL_PORTS.APP_SERVICE_PORTS. AIFA: существующий `/health`
на 8080 (проверяет жизнь API, не готовность всех БД/моделей). Workstation:
`/health/readiness` и `/health/liveness` на 8080.

### Маршрут приложения

Клиент → аутентифицированный API AIFA → transport:9090 → Kafka → supervisor /
agent-runtime. Agent-runtime → внутренний API AIFA и Workstation.
`config-registry` обслуживает `/api/agents`, `/api/owner-settings`, `/api/chats`.
Его маршруты при необходимости маршрутизируются отдельно на config-registry;
WebSocket `/ws`, история и файлы — на transport.

В MU `FINENGINE_OUROBOROS_URL` должен вести на **transport**, а не на порт probes
agent-runtime: `http://<transport-service>:9090`. GigaAgent `AIFA_ENGINE_URL`:
`http://<aifa-service>:8080`. Оба клиента Workstation:
`http://<workstation-service>:8080`. Имена Kubernetes Service нужно взять из
фактически сгенерированного релиза Operator; AGENT_NAME не гарантирует DNS-имя.

AIFA уже посылает `X-Ouroboros-Owner-Id` и `X-Ouroboros-Chat-Id`. transport
использует доверенную идентичность; пользователь не должен попадать на него
напрямую с произвольно заданными заголовками. Default agent `main` создаётся
реестром для владельца при разрешении идентичности — не нужно вручную вставлять
одного общего owner/agent для всех пользователей.

## 3. Конфигурационный репозиторий и Jenkins

Это отдельный от исходников набор настроек, который передаётся CONFIG_REPO.

| Файл по инструкции | Что заполнить для нас |
|---|---|
| COMMON.yaml | hub/aef IDs своей команды; 7 записей agents; git/branch; пути; корпоративные baseImage/runImage; buildCredentials; quality gates |
| agents/stand.yaml | 7 AGENT_NAME, команды, ENV, replicas/resources, probes/service ports |
| integrations/stand.yaml | реальные model/S3/IDP endpoint, PostgreSQL, Kafka; whitelist входа |
| namespace/stand.yaml | cluster/namespace, stand ID/type, Jenkins credentials, SecMan/PG, сертификаты, ingress, логи и наблюдаемость |

В HTML также встречается `namespaces/` вместо `namespace/`: использовать путь,
который ожидает установленная версия pipeline/template, а не создавать оба.
`stand.yaml` — обозначение файла стенда в инструкции; точное имя согласовать с
выбранным STAND и правилами вашей библиотеки Jenkins.

В COMMON пять записей ядра указывают на наш репозиторий и `path: '/gigaagent'`,
AIFA — `path: '/aifa'`, Workstation — `path: '/workstation'` (если сохранена
текущая монорепозиторная раскладка). Не оставлять `/` из примера другого репо.
Для всех сборок использовать корневой Dockerfile соответствующего контекста.
Python-образ взять закреплённый в текущих desc.yaml; baseImage/runImage должны
совпадать с теми, на которых проверяются зависимости и бинарники Workstation.
Не заменять его автоматически на более старый digest из HTML.

В COMMON assembly `aefsdk: false`, `claw: "true"` — значения из инструкции
для Python GigaAgent. Их обработка зависит от библиотеки сборки. Для AIFA и
Workstation ориентироваться на рабочую Python-сборку Workstation контура и
проверить, не переопределяет ли библиотека выбранный Dockerfile/entrypoint.
Данные hub.id, aef.id, aef.module_id — КЭ нашей команды/модуля, не чужого ядра.

| Jenkins | Параметр | Выбор / источник |
|---|---|---|
| CI | CONFIG_REPO | наш репозиторий конфигурации |
| CI | CONFIG_BRANCH | зафиксированная ветка релиза, например main |
| CI | CUSTOM_DOCKERFILE | gigaagent:Dockerfile из инструкции; при пяти именах ядра проверить механизм выбора Dockerfile для каждого alias в используемом pipeline |
| CI | AGENTS | только если нужно переопределить ветки записей COMMON |
| CD | Script Path | jenkins/CD/cd_customResources.groovy из корпоративного CI/CD репо |
| CD | CONFIG_REPO / CONFIG_BRANCH | тот же согласованный набор конфигурации |
| CD | DISTRIB | фактическая ссылка на ZIP, полученный CI |
| CD | STAND | IFT |
| CD | agentsLabel | разрешённый Linux runner вашей команды, например masterLin, если он существует |
| CD | installTimeout | 10m как начальное значение; сверить допустимый формат в job |
| CD | updateRoleBindings | true при первой установке в новый namespace; далее false при сохранённых правах |
| CD | template_version | установленная и разрешённая версия Operator/template; 1.12.0 — только если она реально поддерживается в вашем контуре |

Для namespace также получить реальные PASS/GIT/DROPAPP credential ID,
DROPAPP_CLUSTER, DROPAPP_NAMESPACE, STAND_ID/TYPE, пути tenants SecMan,
клиентские/серверные CN и ingress DNS. Копировать чужие CI/namespace/CN из HTML нельзя.

## 4. Матрица сетевых соединений

Порт назначения ниже — реальный порт сервиса. Порт клиента в mesh может
отличаться: его берут из сгенерированной интеграции/INNERPORT/MESH_PORT.
Все HOST/IP/порт/TLS/CN должны быть подтверждены владельцами ресурсов.

| Откуда | Куда | Сеть/порт | Раздел интеграций | Требуется |
|---|---|---|---|---|
| 5 компонентов ядра | PostgreSQL ядра | TCP, выданный PG порт (часто 5432/5433) | POSTGRES | всегда |
| 5 компонентов ядра | S3 ядра | HTTPS/443 либо выданный порт mesh | MTLS/GIGA согласно TLS ресурса | всегда |
| transport/supervisor/runtime/memory | Kafka сообщений | TCP/TLS, порты брокеров | KAFKA.CLUSTERS | для выбранного kafka backend |
| runtime/supervisor/memory | шлюз моделей | HTTP(S), выданный порт | MTLS/GIGA либо согласованный HTTP маршрут | модели |
| AIFA | transport | HTTP + WebSocket, 9090 | внутренний Service/mesh | всегда |
| runtime | AIFA internal API | HTTP 8080 | внутренний Service/mesh | всегда |
| AIFA/runtime | Workstation | HTTP 8080 | внутренний Service/mesh | выполнение кода |
| AIFA | PostgreSQL AIFA | TCP, выданный PG порт | POSTGRES | всегда |
| AIFA | S3 AIFA | HTTP(S), выданный порт | MTLS/GIGA согласно TLS ресурса | документы/артефакты |
| AIFA | шлюз моделей | HTTP(S), выданный порт | MTLS/GIGA | анализ и планирование |
| AIFA | IDP OCR | HTTP(S), выданный порт | MTLS/GIGA | если используется IDP OCR |
| Workstation | PostgreSQL Workstation | TCP, выданный PG порт | POSTGRES | метаданные сессий |
| приложения/sidecar | Kafka tracing/log/config | TCP/TLS | KAFKA.CLUSTERS + namespace telemetry | по требованиям платформы |
| Jenkins runner | Git/Nexus/SberOSC/registry | SSH/HTTPS/корпоративные порты | доступы build-инфраструктуры | сборка, не runtime egress pod |

Для PG можно согласовать INNERPORT 15432 (ядро), 15433 (AIFA), 15434
(Workstation). Это предлагаемые уникальные виртуальные порты,
не реальные порты серверов. В POSTGRES запись содержит NAME/HOST/IP/PORT/INNERPORT.
Для Kafka пример MESH_PORT 19096 — только если свободен в namespace. Прописать
все advertised broker addresses, доступные mesh, а не только один bootstrap host.

Если PG физически один кластер, интеграции могут совместно использовать маршрут,
но БД/роли/схемы и права остаются раздельными. Внешние TLS-настройки и локальное
плечо mesh не путать: PLAINTEXT допустим только для согласованного защищённого
локального mesh-плеча. Для прямого подключения к TLS/SASL брокерам выбрать
SSL/SASL_SSL и соответствующие OUROBOROS_KAFKA_* credentials/CA/cert/key.

Не добавлять SberChat GRPC, MCP, A2A, Gigasearch/DLP только потому, что они есть
в примере HTML. Они включаются отдельным функциональным решением.
INGRESS_WHITELIST не оставлять `CN: .*` / `PATH: .*` как готовую политику.
Для входа AIFA задать фактический доверенный CN/маршрут. В namespace ingress
ключ маршрута должен совпадать с целевым AGENT_NAME, например aifa.

## 5. Базы, схемы, таблицы, роли

Предлагаемый простой вариант: 3 логические БД на одном корпоративном PG-кластере.
При невозможности — одна БД с тремя схемами и разными ролями, после проверки
search_path/миграций. Пять компонентов ядра делят одну БД/схему ядра.

| Назначение | БД | Схема | Рабочая роль | Инициализация |
|---|---|---|---|---|
| Ядро, все 5 компонентов | ouroboros | ouroboros | ouroboros_app | Alembic head 0034 текущих исходников |
| AIFA | finengine | finengine | finengine_app | модели SQLAlchemy, все 9 хранилищ |
| Workstation | workstation | workstation | workstation_app | Alembic head 0002_workspace_persistence |

Это предлагаемые имена; реальные названия и роли выдаёт DBA. Название schema
AIFA должно состоять из букв/цифр/подчёркиваний, начинаться с буквы/подчёркивания:
длинное имя ресурса с дефисами нельзя автоматически ставить в FINENGINE_PG_SCHEMA.

Попросить DBA создать schema нужному владельцу и установить search_path для
соответствующей роли в нужной БД. Например, `ALTER ROLE ouroboros_app IN DATABASE
ouroboros SET search_path TO ouroboros, public` (аналогично остальным). В AIFA
дополнительно задать `FINENGINE_PG_SCHEMA=finengine`: её движок явно устанавливает
search_path. Для Workstation не рассчитывать на DATABASE_SCHEMA: в текущем
migrations/env.py нет применения этого поля к search_path/version_table_schema;
использовать согласованный search_path роли для миграционного и рабочего подключения.

Установочной роли нужны DDL-права в своей схеме; рабочей — CONNECT/USAGE,
SELECT/INSERT/UPDATE/DELETE и необходимые права на sequences. Если entrypoint
сам мигрирует/создаёт таблицы, его роль должна иметь соответствующие DDL-права
или сначала требуется адаптировать процесс инициализации. Superuser не нужен.
DDL-роль и runtime-роль должны видеть одну и ту же schema/search_path.
`pgvector` для текущей таблицы memory не обязателен: embedding хранится REAL[].

### Ядро GigaAgent

Единственный источник итоговой структуры — `gigaagent/infra/migrations/versions/`.
Не создавать вручную сокращённый набор таблиц. Применяются все миграции до 0034,
а не только «multiuser» 0010. Таблицы вместе с индексами/ограничениями включают:

| Группа | Таблицы/структуры |
|---|---|
| Пользователи в терминах ядра | agents, chats, user_settings, owner_runtime_state |
| Диалоги и выполнение | sessions, messages, tasks, task_plans, task_inputs, context_summaries, tool_invocations, task_episodes, llm_usage, audit_events |
| Координация/изоляция | deployment_keyspace, consolidation_leases, tool_confirmations, security_contexts, workstation_sessions |
| Память | memory, scratchpad_blocks |
| Файлы | folders, files, message_files, file_transfer_leases |
| Расписания | schedules, schedule_runs |
| Расширяемые навыки | skills, skill_revisions, skill_artifact_objects, owner_skill_state, owner_skill_bindings, skill_registry_policy, skill_command_receipts, skill_binding_snapshots, task_skill_bindings, task_skill_binding_revisions, skill_catalogue_snapshots, skill_catalogue_cards, skill_catalogue_loads, skill_activations |
| Интеграции | agent_configuration_announcements, mcp_user_tokens, a2a_user_tokens, personal_mcp_servers, personal_mcp_user_tokens, personal_a2a_agents, personal_a2a_user_tokens |
| Версия схемы | alembic_version |

Функции могут быть выключены, но таблицы всё равно создаются полной миграцией.
Временную `_a2a_context_rekey` из 0033 отдельно не заводить: это миграционная
TEMPORARY-таблица. Для нового контура перед включением всех служб выполнить
один раз в образе ядра `python -m alembic upgrade head`; затем проверить
`python -m alembic current` и `python -m alembic heads` в том же релизе.
Это команды установки в контуре, при подготовке документа они не выполнялись.

### AIFA: одна БД, девять URL, 22 таблицы

Все переменные следующей таблицы указывают на один DSN finengine, а не на девять
серверов. Типы колонок, индексы, NULL и FK уже описаны в [FINENGINE-DATABASE.md](FINENGINE-DATABASE.md).

| Переменная SecMan | Таблицы |
|---|---|
| FINENGINE_AUTH_DATABASE_URL | users, invites |
| FINENGINE_DOCUMENTS_DATABASE_URL | document, document_chunk |
| FINENGINE_DIALOGS_DATABASE_URL | dialog_owner, dialog |
| FINENGINE_RUNS_DATABASE_URL | station_run |
| FINENGINE_STATE_DATABASE_URL | state, analysis_case, analysis_case_document, analysis_case_artifact, analysis_plan_run, analysis_plan_node_run, analysis_override_event, subject_profile_history |
| FINENGINE_JOURNALS_DATABASE_URL | journal |
| FINENGINE_ARTIFACTS_DATABASE_URL | artifact |
| FINENGINE_SCENARIOS_DATABASE_URL | scenarios |
| FINENGINE_NEWS_DATABASE_URL | sources, raw_items, digest_runs, digest_items |

Сначала создать schema. `python -m finengine.store.ensure_schema --apply`
умеет создать schema, но не означает создание всех 22 таблиц. Затем установочным
шагом инициализировать все хранилища по моделям с PK/индексами/ограничениями.
`create_all()` не является универсальной миграцией существующей структуры.
Проверить покрытие всех 9 хранилищ, а не только успешный `/health`.

### Workstation

В перенесённой версии 5 прикладных таблиц: sessions, audit_events,
workspace_scopes, workspace_revisions, workspace_files; плюс alembic_version.
Даже при выключенном WORKSPACE_STORE_ENABLED миграции должны быть полными.
Старое описание «две таблицы Workstation» относится к предыдущей версии.
`app.py` запускает миграции до API. Оставить 1 реплику: rootfs сессии привязан
к pod, общее PostgreSQL-хранилище само по себе не делает файловую систему общей.

### DSN и TLS

Ядро/AIFA: `postgresql+psycopg://USER:PASSWORD@CLIENT_HOST:CLIENT_PORT/DB`.
Workstation: `postgresql+asyncpg://USER:PASSWORD@CLIENT_HOST:CLIENT_PORT/DB`.
Драйверы различаются. CLIENT_HOST/PORT — адрес после согласования mesh.
Реальный пароль кодируется для URL; TLS, CA, client cert/key задаются по
фактическому режиму ресурса. Не переносить sslmode/порт/сертификаты чужого стенда.
Для Workstation параметры должны быть совместимы с asyncpg, не копировать в
его URL psycopg-специфичные `options=-csearch_path...`.

Failover ядра: либо один актуальный OUROBOROS_RUNTIME_DB_URL, либо подряд
OUROBOROS_RUNTIME_DB_URL_1..N (все ведут к одной логической БД, а не к независимым
копиям). Не заменять PostgreSQL строкой SQLite из справочной таблицы HTML —
текущий runtime такой backend не поддерживает.

Бюджет PG connections считать как сумму всех sync/async/probe пулов по всем
репликам. Одинаковые DSN AIFA не означают один общий пул. Лимит соединений,
объёмы, backup/RPO/RTO и retention данных согласовать с DBA по нагрузке.

## 6. Kafka и S3; Redis отключён

### Kafka

Общий префикс примера: `aifa.ift` (для PROM другой). Одинаков на всех пяти
компонентах и в миграционной/настроечной конфигурации деплоя.

| Топик | Назначение |
|---|---|
| aifa.ift.inbound | входящие сообщения, supervisor |
| aifa.ift.outbound | ответы в транспорт |
| aifa.ift.agent-events | события агентного выполнения |
| aifa.ift.task-stops | уведомления об остановке задач |
| aifa.ift.memory-events | фоновые события памяти |

Начальный вариант — по 1 partition: код ряда transport-путей явно использует
partition 0; больше partitions само по себе не увеличит их пропускную способность.
Replication factor 3 — если кластер позволяет. Выдать producer/consumer права
на эти темы и consumer groups этого префикса; окончательный набор ACL согласовать
с Kafka-командой. Retention/max.message.bytes согласовать с размером сообщений
(OUROBOROS_KAFKA_MAX_MESSAGE_BYTES по умолчанию 32 MiB), quota и требованиями данных.
Топики приложения не смешивать с tracing/log/configuration topic.

Отдельно получить topic/ACL для AEF_TRACING_KAFKA_TOPIC,
AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC и namespace FLUENT_BIT APP_LOG/ISTIO_LOGS.
Они могут относиться к разным Kafka-кластерам/mesh-портам, как в инструкции.

### Без Redis / Radish

Redis не заказываем и не включаем сетевую интеграцию. У всех пяти компонентов
ядра OUROBOROS_SETTINGS_CACHE_BACKEND=none: настройки читаются из PostgreSQL,
сообщения идут через Kafka. OUROBOROS_REDIS_URL и FINENGINE_REDIS_URL убрать
из настоящих SecMan-файлов, если они были добавлены раньше.

AIFA работает одной репликой и одним worker с пустым FINENGINE_REDIS_URL:
шина локальная. Увеличивать replicas/workers без общего транспорта нельзя.
Это ограничение AIFA, а не причина требовать Redis для Kafka MU ядра.

### S3

2 отдельные приватные корзины: например `aifa-ift-ouroboros`, `aifa-ift-finengine`.
Имена примеров нужно сверить с выданными. Права приложений: list/get/put/delete
в своей корзине/префиксе, без общего admin доступа. Отдельная корзина Workstation
не нужна при выключенном WORKSPACE_STORE_ENABLED.

Все 5 компонентов ядра должны использовать одинаковые endpoint, bucket,
OUROBOROS_STORAGE_TENANT_ID и OUROBOROS_S3_PREFIX. Начальный tenant `aifa-ift`,
prefix можно оставить пустым в выделенной корзине. Ядро добавляет tenants/<tenant>
к своим ключам. AIFA можно задать FINENGINE_S3_PREFIX=ift/.
При переносе существующих данных не менять tenant/prefix/ключи шифрования
произвольно: runtime фиксирует deployment_keyspace и проверяет совпадение.

## 7. Общий ENV пяти компонентов ядра

Ниже — дополнения/переопределения к текущему AIFA-профилю из gigaagent/desc.yaml.
URL и broker version нельзя подставить без выданных сведений.

```yaml
OUROBOROS_ENV: "production"
OUROBOROS_MULTIUSER: "true"
OUROBOROS_MIGRATE_ON_START: "0"
BROWSER_INGRESS_ENABLED: "true"
OUROBOROS_BROKER_BACKEND: "kafka"
OUROBOROS_KAFKA_BOOTSTRAP_SERVERS: "<issued-kafka-mesh-host:port>"
OUROBOROS_KAFKA_TOPIC_PREFIX: "aifa.ift"
# OUROBOROS_KAFKA_API_VERSION: "<confirmed-major.minor>"
OUROBOROS_SETTINGS_CACHE_BACKEND: "none"
OUROBOROS_SERVER_HOST: "0.0.0.0"
OUROBOROS_SERVER_PORT: "9090"
PYTHONPATH: "/app"
OUROBOROS_REPO_DIR: "/app"
OUROBOROS_APP_ROOT: "/data"
OUROBOROS_DATA_DIR: "/data"
SECMAN_SECRETS_FILE: "/vault/secrets/secret-files/gigaagent-secrets"
SECMAN_WAIT_SEC: "30"
OUROBOROS_DRAIN_TIMEOUT_SEC: "20"
OUROBOROS_MEMORY_DISTILL: "false"
OUROBOROS_TOOL_CONFIRMATION_ENABLED: "false"
OUROBOROS_USER_FILE_TTL_HOURS: "720"
OUROBOROS_STORAGE_TENANT_ID: "aifa-ift"
OUROBOROS_S3_BUCKET: "<issued-ouroboros-bucket>"
OUROBOROS_S3_ENDPOINT: "<issued-s3-client-url>"
OUROBOROS_S3_REGION: "<issued-region>"
OUROBOROS_CHAT_ENCRYPTION_ENABLED: "true"
A2A_ENABLED: "false"
MCP_ENABLED: "false"
DIALOGS_GRPC_ENDPOINT: ""
WORKSTATION_BASE_URL: "http://<workstation-service>:8080"
AIFA_ENGINE_URL: "http://<aifa-service>:8080"
CLOUDRU_FOUNDATION_MODELS_BASE_URL: "<issued-model-base-url>"
OUROBOROS_MODEL: "cloudru::glm-5.2-mip/glm-5.2-fp8"
OUROBOROS_MODEL_LIGHT: "cloudru::qwen/qwen35-397b-a17b-fp8"
OUROBOROS_MODEL_FALLBACK: "cloudru::qwen/qwen35-397b-a17b-fp8"
```

Модели взяты из IFT-профиля AIFAClaw; их наличие и доступы проверить у владельца
шлюза. Для другого стенда использовать соответствующие доступные ID; не копировать
строку `deepseek-v3//scratch/...` из чужого deployment. `OUROBOROS_WORKER_SLOTS=4`
поставить на agent-runtime; у остальных можно оставить 1. Жёсткий timeout задач
5400 секунд и AIFA loop budgets сохранить из текущего профиля.

Сохранить из текущего desc allowlists инструментов/навыков. В частности:
OUROBOROS_USER_ENABLED_SKILL_IDS допускает distribution:workstation,
distribution:aifa-planning, distribution:aifa-plan-author,
distribution:aifa-engine, distribution:aifa-atom-author;
OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS=distribution:aifa-engine.
Эти допуски нужны и при подключённом extension-runtime; их нельзя заменять общей отменой ограничений MU.

Наблюдаемость: AEF_TRACING_ENABLED=true, AEF_TRACING_KAFKA_TOPIC=<issued-topic>
при предоставленной платформой интеграции. На runtime настроить
AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED=true, BOOTSTRAP_SERVERS/TOPIC и
AGENT_SYSTEM_CONFIGURATION_{AGENT_ID,CLUSTER_ID,NAMESPACE,DISTRIBUTIVE,REPO_CODE,
APP_CI,MMS_AGENT_VERSION,VERSION_NO,FILENAME} с данными нашего релиза.
Там, где идентификаторы предоставляет downward API/платформа, не дублировать
чужие значения из HTML.

## 8. SecMan пяти компонентов ядра

Общий файл `gigaagent-secrets` в SECMAN.SECRET_FILES namespace либо эквивалентный
mount; он одинаков для всех пяти служб. JSON-объект или dotenv со следующими ключами:

| Ключ | Что положить |
|---|---|
| OUROBOROS_RUNTIME_DB_URL (либо _1..N) | DSN PG ядра с его ролью/search_path/TLS |
| CLOUDRU_FOUNDATION_MODELS_API_KEY | ключ внутреннего model gateway |
| OUROBOROS_S3_ACCESS_KEY / OUROBOROS_S3_SECRET_KEY | доступ к S3 ядра |
| OUROBOROS_CREDENTIALS_ENCRYPTION_KEY | 64 случайных байта в base64; один ключ у всех компонентов |
| OUROBOROS_CHAT_ENCRYPTION_KEY | 32 случайных байта в base64; один ключ у всех компонентов |
| AIFA_ENGINE_TOKEN | общий с AIFA токен internal tools API |
| WORKSTATION_CREATE_SECRET | общий с Workstation секрет создания сессии |
| OUROBOROS_KAFKA_SASL_PASSWORD | только при выбранном SASL; имя пользователя и протокол задаются отдельно |

Ключи шифрования разного размера, их нельзя путать или менять при каждом деплое.
Для нового стенда генерация: `openssl rand -base64 64` и `openssl rand -base64 32`
соответственно, сохранить в SecMan, не в Git. При переносе данных сохранять ключи.

Существующая поддержка OUROBOROS_SECRETS_DIR (отдельный файл на ключ) сохранена.
Можно использовать namespace SECMAN.PG для выдачи OUROBOROS_RUNTIME_DB_URL_1..N
в этот каталог, а общий файл — для остальных секретов. Тогда указать
OUROBOROS_SECRETS_DIR=/vault/secrets/secret-files. Не дублировать один ключ с
разными значениями: общий SecMan-файл имеет больший приоритет.

Сертификаты ca/client.pem/client.key доставляются отдельными файлами. В путях
DSN/Kafka TLS указывать фактический mount, доступный UID контейнера. SECMAN.PG
в инструкции — это шаблонизация секрета, не создание БД/ролей/таблиц.

## 9. Настройки AIFA

У AIFA нет собственного флага OUROBOROS_MULTIUSER. Многопользовательский контекст
формируется из её users/auth и передаётся в transport от имени конкретного
пользователя. Не добавлять ей команды multiuser_config.* из ядра.

```yaml
FINENGINE_HOST: "0.0.0.0"
FINENGINE_PORT: "8080"
FINENGINE_WORKERS: "1"
FINENGINE_PG_SCHEMA: "finengine"
FINENGINE_OUROBOROS_URL: "http://<transport-service>:9090"
FINENGINE_WORKSTATION_URL: "http://<workstation-service>:8080"
FINENGINE_REDIS_URL: ""
FINENGINE_OPENROUTER_BASE_URL: "<issued-model-base-url>"
FINENGINE_OPENROUTER_MODEL: "glm-5.2-mip/glm-5.2-fp8"
FINENGINE_PLANNER_MODEL: "glm-5.2-mip/glm-5.2-fp8"
FINENGINE_LOCATE_MODEL: "glm-5.2-mip/glm-5.2-fp8"
FINENGINE_READ_VALUE_MODEL: "glm-5.2-mip/glm-5.2-fp8"
FINENGINE_NEWS_CLASSIFY_MODEL: "glm-5.2-mip/glm-5.2-fp8"
FINENGINE_ENFORCE_INTERNAL_AUTH: "1"
FINENGINE_ADMIN_USERNAME: "admin"
FINENGINE_S3_BUCKET: "<issued-finengine-bucket>"
FINENGINE_S3_ENDPOINT: "<issued-s3-client-url>"
FINENGINE_S3_REGION: "<issued-region>"
FINENGINE_S3_PREFIX: "ift/"
FINENGINE_DATA_DIR: "/app/data"
FINENGINE_DOCS_DIR: "/app/data/documents"
SECMAN_SECRETS_FILE: "/vault/secrets/secret-files/aifa-secrets"
SECMAN_WAIT_SEC: "30"
```

FINENGINE_WORKERS здесь фиксирует ожидаемый одно-процессный режим; текущий
entrypoint и так запускает один uvicorn worker и не превращает это ENV в
`--workers`. Число Kubernetes replicas задаётся отдельно. При >1 реплике/worker
обязателен общий FINENGINE_REDIS_URL и проверка фоновых сценариев/восстановления
прогонов; Redis сам по себе не доказывает корректность всех фоновых задач.

Файл SecMan `aifa-secrets`:

- все 9 FINENGINE_*_DATABASE_URL из таблицы выше, с DSN одной БД finengine;
- OPENROUTER_API_KEY — ключ внутреннего model gateway;
- FINENGINE_AUTH_SECRET и FINENGINE_ADMIN_PASSWORD — секрет JWT и пароль bootstrap admin;
- AIFA_ENGINE_TOKEN — то же значение, что у runtime;
- FINENGINE_WORKSTATION_CREATE_SECRET — то же значение, что у Workstation;
- FINENGINE_S3_ACCESS_KEY / FINENGINE_S3_SECRET_KEY;
- FINENGINE_TAVILY_API_KEY — только если разрешён внешний поиск; иначе не задавать.

Для IDP задать FINENGINE_IDP_OCR_URL и сетевой доступ только если этот путь
действительно используется. OCR fallback AIFA требует Tesseract с RU/EN;
сборку RPM-пакетов и CA проверить внутри выбранного корпоративного образа.

## 10. Минимум для Workstation

Оставить штатные Dockerfile/entrypoint из перенесённого контура:
APP_HOST=0.0.0.0, APP_PORT=8080, RUNTIME_MODE=local,
IS_CREATE_SESSION_SECRET=true, WORKSPACE_STORE_ENABLED=false.
Секреты в workstation-secrets: DATABASE_URL (asyncpg), SESSION_CREATE_SECRET.
Ключи AWS нужны только при включении workspace store. Корпоративные Auth/Kafka/
Redis возможности Workstation не включать автоматически из расширенного примера.

Обязательное равенство: Workstation SESSION_CREATE_SECRET =
GigaAgent WORKSTATION_CREATE_SECRET = AIFA FINENGINE_WORKSTATION_CREATE_SECRET.
Эти разные имена параметров обозначают одно согласованное значение.

## 11. Порядок подготовки и приёмки

1. Согласовать с платформой namespace, КЭ/IDs, credentials Jenkins, Operator/template,
   ingress DNS/CN, build mirrors и registry. Взять реальные PG/S3/Kafka/model данные.
2. Заказать БД/роли/схемы, 2 S3 buckets, 5 app Kafka topics + telemetry topics/ACL,
   маршруты и сертификаты; Redis в этот профиль не входит.
3. Адаптировать подготовленные COMMON и отдельные MU agents ENV/args из
   gigaagent/deploy/mu/; включить один миграционный шаг ядра до запуска служб.
4. Заполнить конфиг-репозиторий и SecMan, сверить разные форматы секретов и mounts.
5. Собрать дистрибутив; применить PG-миграции и инициализацию AIFA в согласованном
   порядке. Включить все компоненты только после готовности схемы.
6. Проверить probes, реальную доступность PG/S3/Kafka/модели из pod,
   появление 9 инструментов aifa-engine в agent-runtime и команду в Linux Workstation.
7. Проверить два разных пользователя: раздельные чаты, файлы, настройки и задачи;
   остановку, восстановление после рестарта, маршрутизацию ответа на нужный transport.
8. Только после этого увеличивать replicas/slots и проводить нагрузочную приёмку.

Для ядра нужны `/data` и `/tmp`, для AIFA `/app/data`, для Workstation `/tmp`;
права SecMan-монтов должны подходить UID/GID. Не заменять общие PG/S3 локальными
томами pod и не считать настройку 1 replica вариантом HA.

## 12. Ошибки/несовпадения примера HTML, которые не переносить

- В таблице 8.25 broker-переменная набрана с кириллицей; правильно
  `OUROBOROS_BROKER_BACKEND` полностью латиницей.
- Утверждение о SQLite fallback ядра устарело: runtime требует PostgreSQL.
- OUROBOROS_REDIS_SENTINEL_SERVICE не читается, имя Sentinel — в URI.
- OUROBOROS_MODEL_CODE и OUROBOROS_STORAGE_BACKEND устарели для текущего кода.
- `/tmp/repo` не содержит наши навыки: в текущем образе OUROBOROS_REPO_DIR=/app.
- Agent-runtime на 9090 обслуживает probes, пользовательский WebSocket живёт на transport.
- В примере namespace route-name gigaagent комментируется как transport;
  маршрутизировать нужно по фактическому AGENT_NAME, не названию домена.
- true для distillation, включённые MCP/Dialogs и 16 slots — настройки чужого
  стенда, не обязательные требования к AIFA.
- Пример COMMON не перечисляет workstation, хотя agents-пример его запускает:
  включить Workstation в собираемую поставку явно.
- Ссылки на разные версии установки/шаблонов, чужие IP/CN/IDs и разные написания
  namespace(s) сверять с реально установленной библиотекой платформы.

## Источники в нашем коде

- `gigaagent/multiuser_config/agent_runtime/run.py`, `new_core/tools/providers/extension.py` — подключение и каталог расширений.
- `gigaagent/ouroboros/app/broker_composition.py` — backends, streams/topics и refusal inprocess в MU tiers.
- `gigaagent/infra/cache/__init__.py`, `infra/redis_client.py` — кеш и Sentinel URI.
- `gigaagent/ouroboros/storage/settings/{schema,bootstrap,secman}.py` — поддерживаемые ENV/секреты.
- `gigaagent/infra/migrations/versions/` — схема ядра, включая 0034.
- `aifa/finengine/src/finengine/config.py`, `store/{engine,bus,ensure_schema}.py` — schema/Redis/replicas.
- `workstation/migrations/`, `app/core/secrets.py` — схема и SecMan перенесённого Workstation.
