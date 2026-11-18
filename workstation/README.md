> Эта версия перенесена из AIFAClaw/workstation без изменения runtime-кода.
> Jenkins, SecMan и подключение к Ouroboros/AIFA: `../aifa/deploy/README.md`.
> Профиль этой поставки: `deploy/configmap.ouroboros.example.yaml`.
> Исходная документация Workstation контура приведена ниже.

# Workstation Sandbox Service

FastAPI-сервис для управления изолированными sandbox-сессиями. Сервис создаёт сессии, выполняет команды, работает с файлами внутри rootfs сессии и ведёт аудит операций.

Основные runtime-режимы:
- `local` — запуск через собственный wrapper `ws-sandbox`
- `bwrap` — основной runtime по аналитике через `systemd-run --user --scope -- bwrap`

По умолчанию сервис стартует с `RUNTIME_MODE=local`. Для окружений, где
доступны namespace/mount-системные вызовы и кастомный seccomp-профиль, можно
использовать `RUNTIME_MODE=bwrap`.

## Возможности

- идемпотентное создание сессии по `X-Session-Id`
- bearer-auth, привязанный к session id
- sync exec и stream exec
- upload, download и list файлов
- policy engine для команд, env и путей
- файловые и exec-квоты
- аудит действий в БД
- idle cleanup и startup reconcile
- опциональное durable-сохранение `/workspace` в S3
- OpenAPI/Swagger и системные endpoint'ы

## Стек

- Python 3.12
- FastAPI + Uvicorn
- Pydantic v2
- SQLAlchemy async + Alembic
- APScheduler
- SQLite для dev/smoke, Postgres для production

## Runtime-модель

### `local`

Текущая реализация запускает пользовательскую команду через C-wrapper
`ws-sandbox`.

Особенности:
- rootfs для сессии создаётся копированием из `ROOTFS_TEMPLATE_DIR`
- wrapper получает `--session-dir`, `--cwd`, лимиты ресурсов, env-file и
  exec-allowlist
- перед `execve` wrapper применяет process group, rlimits, close-fd,
  `NO_NEW_PRIVS`, Landlock, `PDEATHSIG`, `DUMPABLE=0`, seccomp и чистое env
- cgroup v2 limits применяются best-effort, если `/sys/fs/cgroup` делегирован
  текущему uid
- сетевая изоляция не реализуется wrapper-ом и должна задаваться отдельно
  через pod-level `NetworkPolicy`

### `bwrap`

Целевой production runtime по текущей аналитике:

```bash
systemd-run --user --scope --quiet -- bwrap ...
```

Особенности:
- bind rootfs в `/`
- `--proc /proc`, `--dev /dev`, `--tmpfs /tmp`
- `--share-net` или `--unshare-net`
- resource limits через `systemd-run`

Важно:
- unit-контракт команды уже покрыт тестами
- живой end-to-end прогон `bwrap + systemd-run` нужно делать на Linux-стенде
- `/health/readiness` теперь проверяет runtime prerequisites, включая template root и preflight целевого runtime

## API

Все ручки находятся под `/v1`.

### Системные endpoint'ы

- `GET /health/liveness`
- `GET /health/readiness`
- `GET /version`
- `GET /metrics`

### Аутентификация

- `POST /v1/sessions` не требует bearer token
- все остальные session endpoint'ы требуют `Authorization: Bearer <token>`
- все остальные session endpoint'ы требуют `X-Session-Id: <session-id>`

Токен возвращается только один раз при создании новой сессии. В БД хранится только `sha256(token)`.
`X-Session-Id` должен быть безопасным opaque-id из букв, цифр, `.`, `_`, `-`.

### Session endpoint'ы

- `POST /v1/sessions` — создать сессию или переиспользовать `ready`-сессию
- `GET /v1/sessions/{session_id}` — получить состояние сессии
- `POST /v1/sessions/{session_id}:terminate` — завершить сессию

### Exec endpoint'ы

- `POST /v1/sessions/{session_id}/exec` — синхронное выполнение команды
- `POST /v1/sessions/{session_id}/exec:stream` — NDJSON stream stdout/stderr/exit events

### File endpoint'ы

- `POST /v1/sessions/{session_id}/files:upload`
- `POST /v1/sessions/{session_id}/files:download`
- `POST /v1/sessions/{session_id}/files:list`

## Основные request/response формы

### Create session

Заголовок:

```http
X-Session-Id: example-session
```

Тело:

```json
{
  "profile": "default",
  "idleTimeoutSec": 900,
  "absoluteTtlSec": 3600,
  "networkEnabled": false
}
```

Ответ:

```json
{
  "id": "example-session",
  "status": "ready",
  "token": "...",
  "createdAt": "2026-04-23T08:00:00Z",
  "expiresAt": "2026-04-23T09:00:00Z",
  "profile": "default",
  "networkEnabled": false,
  "quotas": {
    "maxFileBytes": 20971520,
    "maxTotalBytes": 104857600,
    "execTimeoutSec": 300
  }
}
```

### Exec

```json
{
  "command": ["bash", "-lc", "pwd && echo ok"],
  "cwd": "/workspace",
  "env": {},
  "stdin": null,
  "stdinEncoding": "utf8",
  "timeoutSec": 30
}
```

В `RUNTIME_MODE=local` короткое имя executable (`bash`, `ls`, `cat`, ...)
резолвится на стороне сервиса через `EXEC_ALLOWLIST`, например `bash` →
`/bin/bash`. Пути с `/` (`/bin/bash`, `./x`, `tools/x`) передаются как есть.

Ответ:

```json
{
  "exitCode": 0,
  "stdout": "/workspace\nok\n",
  "stderr": "",
  "stdoutTruncated": false,
  "stderrTruncated": false,
  "durationMs": 12,
  "timedOut": false
}
```

### File list

```json
{
  "path": "/workspace",
  "recursive": true,
  "maxDepth": 5,
  "includeSha256": true
}
```

## Ошибки

Сервис использует единый JSON-формат:

```json
{
  "code": "invalid-state",
  "message": "busy",
  "details": {
    "sessionId": "example-session"
  }
}
```

Основные коды:
- `bad-request`
- `auth-required`
- `policy-deny`
- `not-found`
- `invalid-state`
- `quota-exceeded`
- `timeout`
- `internal`

Для `POST /v1/sessions` возможен `409 invalid-state`, если сессия с тем же id уже находится в `provisioning` или `terminating`.

## Конфигурация

Ключевые переменные окружения:

- `SERVICE_NAME`
- `APP_TITLE`
- `APP_WORKERS`
- `APP_IS_RELOAD`
- `BACKEND_CORS_ORIGINS`
- `DATABASE_URL`
- `DATABASE_SCHEMA`
- `DATABASE_ECHO`
- `DATABASE_POOL_SIZE`
- `DATABASE_MAX_OVERFLOW`
- `ROOTFS_TEMPLATE_DIR`
- `ROOTFS_ROOT`
- `RUNTIME_MODE`
- `SANDBOX_BINARY`
- `BWRAP_BINARY`
- `SYSTEMD_RUN_BINARY`
- `MAX_FILE_SIZE`
- `MAX_TOTAL_BYTES`
- `MEMORY_LIMIT_BYTES`
- `CPU_QUOTA_PCT`
- `MAX_TASKS`
- `SESSION_MEMORY_MAX`
- `SESSION_PIDS_MAX`
- `SESSION_CPU_QUOTA_PCT`
- `SESSION_RLIMIT_AS`
- `SESSION_RLIMIT_FSIZE`
- `SESSION_RLIMIT_CPU_SEC`
- `SESSION_RLIMIT_NOFILE`
- `EXEC_ALLOWLIST`
- `EXEC_TIMEOUT_SEC`
- `EXEC_OUTPUT_LIMIT_BYTES`
- `SIGTERM_GRACE_SEC`
- `SESSION_IDLE_TIMEOUT_SEC`
- `SESSION_ABSOLUTE_TTL_SEC`
- `CLEANUP_INTERVAL_SEC`
- `WORKSPACE_STORE_ENABLED`
- `WORKSPACE_STORE_BACKEND`
- `WORKSPACE_STORE_S3_BUCKET`
- `WORKSPACE_STORE_S3_PREFIX`
- `WORKSPACE_STORE_S3_ENDPOINT_URL`
- `WORKSPACE_STORE_RESTORE_TIMEOUT_SEC`
- `WORKSPACE_STORE_SNAPSHOT_TIMEOUT_SEC`
- `ENV_ALLOWLIST`
- `DENY_COMMAND_TOKENS`
- `METRICS_ENABLED`
- `APP_HOST`
- `APP_PORT`

Durable `/workspace` выключен по умолчанию. При `WORKSPACE_STORE_ENABLED=false`
S3-клиент не создаётся даже при `WORKSPACE_STORE_BACKEND=s3`. Для S3 включите
`WORKSPACE_STORE_ENABLED=true` и задайте `WORKSPACE_STORE_S3_BUCKET`.

Ключи `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` /
`AWS_REGION` передаются в boto3 явно из `Settings`, а не через окружение
процесса. Если они не заданы, работает штатная цепочка boto3 (роль инстанса,
`~/.aws`).

### Секреты

Секреты приходят из secman-файла (`SECMAN_SECRETS_FILE`, по умолчанию
`/vault/secrets/secret-files/workstation-secrets`; dotenv или плоский JSON) и
живут только в памяти процесса: `Settings` получает их как init-kwargs, которые
в pydantic-settings приоритетнее env и `.env`. В `os.environ` они не
записываются — иначе они были бы видны в `/proc/<pid>/environ` и наследовались
бы каждым дочерним процессом, включая песочные сессии. Приоритет:
secman-файл > env из configmap > `CONFIG`/`.env` > дефолты в коде. Пустые
значения (`KEY=`) игнорируются, отсутствие файла — no-op (локальная разработка).

Полный пример конфигурации: [.env.example](.env.example)

## Локальный запуск

### 1. Установка зависимостей

```bash
uv venv --python 3.12
./.venv/bin/pip install -e ".[dev]"
```

### 2. Миграции

```bash
./.venv/bin/python server.py migrate
```

### 3. Запуск сервера

```bash
./.venv/bin/python server.py runserver --reload
```

Swagger:
- [http://127.0.0.1:8080/docs](http://127.0.0.1:8080/docs)

## Docker smoke для `local`

```bash
docker build -t workstation-bwrap-local-swagger .
docker run -d --name workstation-bwrap-local-swagger-18080 -p 18080:8080 workstation-bwrap-local-swagger
```

Swagger:
- [http://127.0.0.1:18080/docs](http://127.0.0.1:18080/docs)

Этот контейнер рассчитан на `local` runtime через `/usr/local/bin/ws-sandbox`.
Rootfs-template внутри образа использует кастомный статический BusyBox из
`wrapper/busybox.config`; наружу выставлены только `bash`, `cat`, `ls`, `pwd`,
`grep`, `mkdir`, `touch` и `echo`. `exec` доступен как shell builtin внутри
`bash`, отдельного executable для него нет. Опасные applets вроде `mount`,
`chroot`, `unshare`, `ip`, `nc`, `wget`, `kill`, `ps` и `top` не вкомпилированы.

## Проверки

Полный локальный набор:

```bash
./.venv/bin/ruff check .
./.venv/bin/python -m pytest -q
```

### Что уже проверено

- Python test suite: `61 passed`, `2 skipped` на macOS из-за Linux-only wrapper checks
- wrapper integration tests в Debian container: `7 passed`
- `docker build -t workstation-bwrap:ws-sandbox .`
- финальный image содержит исполняемый `/usr/local/bin/ws-sandbox`; `ldd`
  сообщает `not a dynamic executable`
- `ws-sandbox` в Docker image собирается через musl + static libseccomp и
  укладывается в лимит ≤ 300 KB
- финальный image содержит кастомный BusyBox из `wrapper/busybox.config`;
  rootfs-template содержит только allowlisted applet links, а
  `mount/chroot/unshare/ip/nc/wget/kill/ps/top` отсутствуют в applet list
- `local + fake_sandbox` host-side smoke для сервисных тестов
- Linux wrapper smoke: basic exec, Landlock FS block, seccomp kill policy
- порядок и контракты OpenAPI/Swagger
- lifecycle, exec, stream, files, cleanup, reconcile

### Что ещё нужно отдельно проверять

- writable cgroup v2 delegation и `pids.max`/`memory.max`/`cpu.max`
- живой `bwrap + systemd-run --user --scope` на Linux-стенде
- интеграцию AEFClaw-side plugin/adapter с реальным OpenClaw Gateway repo

## AEFClaw plugin и adapter

Plugin, custom sandbox backend и bridge/adapter теперь являются AEFClaw-side
модулями, а не частью сервиса `workstation-bwrap`.

Граница ответственности:

- этот репозиторий предоставляет только HTTP API и runtime isolation;
- AEFClaw владеет подключаемыми модулями: adapter, custom sandbox backend и
  agent-facing config;
- общий контракт между ними фиксируется через REST/OpenAPI, документацию и
  интеграционные тесты, а не через импорт кода из этого репозитория.

Текущая AEFClaw-side структура:

- `AEFClaw/extensions/workstation/src/adapter.ts` — HTTP transport, token
  registry, retry/error mapping, session strategy;
- `AEFClaw/extensions/workstation/src/backend.ts` — custom sandbox backend
  registration, manager, handle, seed, fs wiring;
- `AEFClaw/extensions/workstation/src/bridge-cli.ts` — local exec bridge для
  `/exec:stream`;
- agent выбирает этот backend обычной AEFClaw config-настройкой
  `agents.*.sandbox.backend: "workstation"`.

### Стратегия сессий на стороне AEFClaw

AEFClaw-side adapter не хардкодит одну вечную модель session identity. Вместо
этого у него есть явная настройка:

- `sessionStrategy: "ephemeral" | "recover"`

Текущий прототипный default:

- `ephemeral`

Смысл:

- `ephemeral`
  - `sessionId` стабилен только внутри одного живого процесса Gateway
  - после рестарта Gateway создаются новые сессии
  - старые orphan-сессии сервис потом удаляет по TTL
- `recover`
  - оставляет место под будущую recovery-семантику
  - использует deterministic `scopeKey -> sessionId`
  - сам по себе пока не решает проблему lost token без persisted state или
    нового server-side контракта

Это позволяет не хардкодить сегодняшнее решение про потерю сессии навсегда:
сейчас мы просто явно выбираем простую стратегию `ephemeral`.

### Что важно для контракта сервиса

- `POST /v1/sessions` сейчас публичный и возвращает token только при создании
  новой сессии.
- Все остальные session endpoint'ы требуют bearer token и `X-Session-Id`.
- Persisted token recovery между рестартами Gateway намеренно не решается на
  стороне сервиса в текущем прототипе.
- Default AEFClaw strategy — `ephemeral`: после рестарта Gateway создаётся
  новая сессия, старая доживает до service TTL.
- Если в будущем понадобится recover без потери сессии, нужно отдельно
  согласовать новый публичный контракт: persisted entry в AEFClaw или
  server-side token regeneration endpoint.

Проверки plugin/adapter выполняются в репозитории AEFClaw. Здесь достаточно
проверять стабильность HTTP API и OpenAPI schema.

## Известные ограничения

- текущий Docker smoke покрывает только `local` runtime
- для честного `bwrap` smoke нужен Linux-стенд с `bwrap` и `systemd-run --user`
- `sqlite` подходит для dev/smoke; production следует ориентировать на Postgres
- Swagger UI тянет внешние assets с CDN
- `DENY_COMMAND_TOKENS` проверяет argv tokens и типовые shell payload’ы вида `sh -c` / `bash -c`, но это всё ещё не полноценный shell parser
- `files:list` по умолчанию считает `sha256` для каждого файлового узла; для больших деревьев клиенту лучше передавать `includeSha256=false`

## Структура проекта

- `app/core/` — конфигурация, DB, lifespan, setup
- `app/services/runtime/` — runtime adapter и builders для `ws-sandbox`/`bwrap`
- `wrapper/` — C-wrapper `ws-sandbox`, Makefile, Linux security steps
- `app/models/`, `app/repositories/`, `app/schemas/`, `app/serializers/` — основные данные и преобразования session/audit слоёв
- `app/services/session/` — session service, factory и stream encoder
- `app/services/` — policy engine, file gateway, locks, periodic tasks, state machine
- `app/routers/v1/` — верхнеуровневая сборка versioned API
- `tests/` — unit, API и runtime smoke tests

## Связанные документы

- [NAVIGATION.md](NAVIGATION.md)
