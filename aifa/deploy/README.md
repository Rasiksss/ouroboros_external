# Корпоративный build/deploy через Jenkins

Состав поставки: `aifa/` (Finengine, порт 8080), `gigaagent/` (Ouroboros,
порт 8080), `workstation/` (Workstation из AIFAClaw, порт 8080).
Фронт не развёртывается. Jenkins job и его общая библиотека находятся на стороне
корпоративной платформы; отдельный Jenkinsfile в этот репозиторий не добавляется.
Docker Compose не является частью этого процесса.

## Распределённый MU

Для пяти отдельных компонентов ядра используйте подготовленные примеры
`gigaagent/deploy/mu/` и [памятку MU](MU-DEPLOYMENT-GUIDE.md).
Это 7 логических компонентов из 3 build context. AGENT_CUSTOM_ARGS задаёт
отдельные команды ядра, AIFA обращается к transport:9090. Перед стартом ядра
нужен один миграционный шаг, рабочие pod запускаются с
OUROBOROS_MIGRATE_ON_START=0. Корневой Dockerfile без этих переопределений
по-прежнему запускает единый сервер на 8080.

## Сборка

У каждого сервиса контекст сборки — его каталог. Используются корневые
`Dockerfile`, `desc.yaml`, `requirements.txt`, `entrypoint.sh` и `.dockerignore`.
Pipeline передаёт `app.baseImage` / `app.runImage` как `BUILD_IMAGE` / `RUN_IMAGE`
и учётные данные SberOSC/Nexus. В дескрипторах закреплён корпоративный Python 3.12
из Workstation контура. Настоящие токены не записываются в исходники.

Workstation перенесён из `AIFAClaw/workstation` вместе с runtime, миграциями,
бинарниками и тестами. Предыдущая версия сохранена в
`deferred/workstation-before-contour/`; в поставку этот каталог не входит.
Новый Workstation уже выполняет миграции через `app.py`, добавлять второй
миграционный entrypoint не нужно. Entrypoint GigaAgent выполняет Alembic перед
запуском сервера. AIFA создаёт схемы своих подсистем при старте.

## Подключения платформы

`app.connections` описывает логические alias для привязки к ресурсам контура.
Имена alias совпадают с переменными окружения, читаемыми runtime. В настройках
релиза нужно привязать их к фактическим endpoint и доставить эти значения в
ConfigMap/окружение. Публичные адреса и Compose DNS для внешних сервисов не используются.

| Сервис | Alias endpoint |
|---|---|
| AIFA | FINENGINE_OUROBOROS_URL, FINENGINE_WORKSTATION_URL, FINENGINE_OPENROUTER_BASE_URL, FINENGINE_IDP_OCR_URL, FINENGINE_S3_ENDPOINT |
| GigaAgent | AIFA_ENGINE_URL, WORKSTATION_BASE_URL, CLOUDRU_FOUNDATION_MODELS_BASE_URL, OUROBOROS_S3_ENDPOINT |
| Workstation | S3_URL (сохранён из контура; нужен при включении S3 workspace store) |

Логические подключения PostgreSQL: `aifa-postgresql`, `gigaagent-postgresql`,
`workstation-postgresql`. Их нужно связать с выделенными ресурсами вашего релиза;
это не выданные платформой идентификаторы БД. Полные DSN с паролями доставляются
через SecMan. У каждого сервиса своя БД/роль, у AIFA и GigaAgent свои S3-корзины.
Имеющийся alias S3 Workstation сохранён из контура; его привязку нужно проверить
перед включением хранения workspace.

Внутренние адреса по умолчанию:

| Направление | Адрес |
|---|---|
| GigaAgent → AIFA | http://aifa:8080 |
| AIFA → GigaAgent | http://gigaagent:8080 |
| Оба клиента → Workstation | http://workstation:8080 |

Если имена Service/маршруты платформы другие, переопределите соответствующие
alias. Для gateway моделей укажите полный внутренний OpenAI-совместимый base URL
(обычно с `/v1`). Проверьте маршруты mesh/egress и доверенные CA базового образа.

## Окружение и секреты

Несекретные параметры находятся в `app.envs` и примерах ConfigMap:

- `aifa/deploy/configmap.example.yaml`;
- `gigaagent/deploy/configmap.example.yaml`;
- `workstation/deploy/configmap.ouroboros.example.yaml`.

Значения `<issued-internal-endpoint>` в примерах нужно заменить настройками
подключений релиза. Не копируйте эти заглушки в рабочую конфигурацию.
Профили моделей `dev/ift/prom/psi` в desc.yaml взяты из AIFAClaw. Jenkins должен
выбрать профиль соответствующего окружения, как в рабочем проекте. При ручном
применении ConfigMap добавьте скалярные параметры выбранного профиля из desc.yaml.

SecMan монтирует отдельный файл для каждого сервиса:

| Сервис | SECMAN_SECRETS_FILE | Шаблон содержимого |
|---|---|---|
| AIFA | /vault/secrets/secret-files/aifa-secrets | aifa/deploy/aifa-secrets.example |
| GigaAgent | /vault/secrets/secret-files/gigaagent-secrets | gigaagent/deploy/gigaagent-secrets.example |
| Workstation | /vault/secrets/secret-files/workstation-secrets | workstation/deploy/workstation-secrets.example |

Формат — dotenv `KEY=value` либо плоский JSON. Непустые секреты перекрывают
ConfigMap; пустые не затирают его. AIFA и GigaAgent читают файл при загрузке
настроек, ждут до SECMAN_WAIT_SEC (по умолчанию 30 секунд) и отказываются
стартовать, если явно настроенный файл не появился/пуст. В локальных тестах
без SECMAN_SECRETS_FILE загрузка файла отключена.

Workstation использует исходный загрузчик контура: JSON/dotenv читается один раз
в память. При отсутствии файла применяется окружение; при включённой проверке
секрета и отсутствии SESSION_CREATE_SECRET запуск Settings завершится ошибкой.
Секреты не экспортируются в os.environ и не наследуются процессами песочницы.
После изменения секрета нужно перезапустить pod.

Обязательные совпадения значений:

- `SESSION_CREATE_SECRET` Workstation = `WORKSTATION_CREATE_SECRET` GigaAgent =
  `FINENGINE_WORKSTATION_CREATE_SECRET` AIFA;
- `AIFA_ENGINE_TOKEN` должен совпадать у AIFA и GigaAgent.

У Workstation включён `IS_CREATE_SESSION_SECRET=true`. Оба клиента передают
секрет в `X-Session-Create-Secret` при создании комнаты, а последующие запросы
используют bearer-токен выданной сессии. Значение
`WORKSTATION_SESSION_CREATE_SECRET` из OpenClaw не является именем настройки
Ouroboros — для него используется `WORKSTATION_CREATE_SECRET`.

У AIFA заполните все FINENGINE_*_DATABASE_URL из шаблона: это подсистемы одной
выделенной БД. У GigaAgent нужны runtime DSN, S3-ключи, ключ модели и
OUROBOROS_CREDENTIALS_ENCRYPTION_KEY (ровно 64 случайных байта в base64).
FINENGINE_AUTH_SECRET и FINENGINE_ADMIN_PASSWORD обязательны для рабочего входа.
Плейсхолдеры CHANGE_ME в примерах не являются готовыми секретами.

## Workstation и хранилища

S3 workspace persistence сохранён в коде контура, но в baseline выключен:
клиенты Ouroboros/AIFA создают временные сессии, файлы приложения сохраняют
своими средствами. Не включайте WORKSPACE_STORE_ENABLED автоматически:
для этого нужны отдельные bucket/credentials и согласованная передача scope.
Kafka/Redis/Auth интеграции исходного Workstation тоже не включаются автоматически;
`configmap.example.yaml` контура — расширенный справочник, а активный пример
для этой поставки — `configmap.ouroboros.example.yaml`.

Платформа должна предоставить записываемые `/data` GigaAgent, `/app/data` AIFA,
`/tmp` Workstation и права чтения SecMan-файлов непривилегированными пользователями.
У AIFA сохранён UID 10002, у GigaAgent 10001; Workstation использует пользователя
`aef` из исходного Dockerfile контура. Учитывайте это в securityContext/fsGroup.
Порты агентов/станции не должны быть доступны пользователям напрямую.

## Проверка релиза

После сборки в контуре проверьте миграции PostgreSQL, чтение SecMan, доступ к
корзинам и модели, health/readiness платформы и реальное выполнение команды
в Linux-песочнице. Бинарники Workstation рассчитаны на Linux x86-64; локальные
API-тесты на Windows не проверяют Landlock/seccomp и запуск этих бинарников.
Старые Compose-файлы/env-примеры оставлены как отдельные материалы локального
стенда и не задают параметры Jenkins-релиза.


## PostgreSQL из платформенного TOML SecMan

Файл из `SECMAN_SECRETS_FILE` (на стенде `/vault/secrets/secret-files/databases-aifa-v1`)
может содержать секцию `[connections.POSTGRES-DB]`, как в `demo-fastapi-db`.
AIFA использует синхронный драйвер `postgresql+psycopg` для всех девяти хранилищ.

- Непустой `url` имеет приоритет над отдельными полями и `query`.
- Если `url` пуст или отсутствует, DSN собирается из `dialect`, `host`, `port`,
  `database`, `username`, `password` и необязательной строки `query`.
- `query` содержит параметры psycopg/libpq, например `sslmode=require&connect_timeout=10`.
  Для `verify-full` укажите выданный CA через `sslrootcert` и имя сервера,
  соответствующее сертификату. SSL-режим автоматически не подменяется.
- Поле `password` по умолчанию исходное (`SECMAN_POSTGRES_PASSWORD_ENCODING=raw`).
  В корпоративных desc задано `SECMAN_POSTGRES_PASSWORD_ENCODING=url`: поле
  декодируется ровно один раз через `unquote` (символ `+` сохраняется), затем
  DSN безопасно собирается через SQLAlchemy. Двойного декодирования нет.
  На готовый `url` этот флаг не влияет: реквизиты в URL уже URL-кодированы.
- `defaultSchemaName` обязателен; он задаёт `FINENGINE_PG_SCHEMA` и `search_path`.
  Прочие параметры `options` сохраняются. Схема должна существовать заранее.
- Остальные параметры подключения, в том числе SSL, сохраняются из `url`/`query`.
  Наличие корректного DSN не подтверждает актуальность пароля или доступность БД.

После обновления образа пересоздайте pod AIFA и проверьте прохождение startup.
Реальные реквизиты в репозитории и диагностических выводах не публикуются.
