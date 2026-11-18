# GigaAgent / Ouroboros

В этом каталоге находятся все исходники runtime Ouroboros, навыки, prompts,
security_config, проверка версии схемы PostgreSQL и зависимости. Имена Python-пакетов не менялись:
`ouroboros`, `new_core`, `supervisor`, `transport`, `infra` и остальные импортируются
как прежде. Внутри образа каталог приложения по-прежнему /app.

## Образы

- `Dockerfile` и `desc.yaml` — корпоративная сборка по схеме Workstation:
  BUILD_IMAGE/RUN_IMAGE из baseImage/runImage, тот же закреплённый Python 3.12,
  TOKEN_OSC и NEXUS_TOKEN_USERNAME/NEXUS_TOKEN_PASSWORD (либо NEXUS_USERNAME/NEXUS_PASSWORD).
  Sonar key — `aif-agents-aef-gigaagent`; проект должен быть доступен pipeline.
  Зависимости requirements.txt и SberChat SDK устанавливаются через SberOSC/Nexus.
  Ошибка установки останавливает сборку. Учётные данные объявлены только в builder.
- `Dockerfile.mu` — отдельный прежний рецепт для выделенных multi-user tiers.
- `Dockerfile.public` — отдельный публичный рецепт для разработки; действующие
  Compose-файлы его не используют.

Контекст сборки — только `gigaagent/`, не корень репозитория.
CI передаёт BUILD_IMAGE/RUN_IMAGE и доступы к корпоративным репозиториям.
Сервер запускается через `entrypoint.sh` после успешного Liquibase-шагa AIFA,
слушает `OUROBOROS_SERVER_HOST`/`OUROBOROS_SERVER_PORT` (по умолчанию 0.0.0.0:8080).
Каталог `/data` должен быть доступен для записи; Compose подключает том.
Chromium в базовой корпоративной сборке не устанавливается.

## Jenkins-деплой

Основная инструкция: `aifa/deploy/README.md`. Подключения платформы и профили
окружений описаны в `desc.yaml`, несекретный пример —
`deploy/configmap.example.yaml`, секреты — `deploy/gigaagent-secrets.example`.
Реальные значения доставляются SecMan в файл `SECMAN_SECRETS_FILE`.
Загрузчик поддерживает JSON/dotenv, сохраняет значения в памяти процесса и
не экспортирует их в окружение. Плагин aifa-engine использует тот же источник
настроек, включая AIFA_ENGINE_TOKEN. Старый формат OUROBOROS_SECRETS_DIR
(по одному файлу на ключ) сохранён; полный SecMan-файл имеет больший приоритет.

Workstation перенесён из AIFAClaw. Для создания сессий GigaAgent передаёт
WORKSTATION_CREATE_SECRET, равный SESSION_CREATE_SECRET на Workstation.
Для доступа к AIFA укажите AIFA_ENGINE_URL; AIFA_ENGINE_TOKEN должен совпадать
с токеном внутреннего API движка. Без правильного ключа
OUROBOROS_CREDENTIALS_ENCRYPTION_KEY процесс не стартует.

Корпоративные сертификаты, ключи Dialogs/GigaChat, MCP/A2A и Kafka задаются
конфигурацией конкретного контура. Справочник настроек сохранён в Other/docs/SETTINGS.md;
он не является частью образа. История разработки и governance находятся в Other/.

Распределённый MU по инструкции разработчиков: `deploy/mu/README.md`.
Подготовлены пять команд запуска ядра, AIFA/Workstation, шаблоны Operator
и явное отключение миграций на рабочих pod после однократной миграционной стадии.

## Структура PostgreSQL

Создаётся только на этапе деплоя через `aifa/liquibase/changelog.sql`.
В GigaAgent нет Alembic и DDL при старте. Сервисы запускаются после Liquibase
под ролями с USAGE на схему и правами чтения/записи данных, без CREATE/владения
таблицами. Проверка `infra/runtime_schema.py` читает маркер версии 34.
