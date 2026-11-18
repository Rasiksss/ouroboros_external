# MU по инструкции разработчиков GigaAgent v6

Это примеры для отдельного конфигурационного репозитория AEF Operator.
В исходном монорепозитории находятся 3 build context; в COMMON — 7 компонентов,
из которых пять собираются из `gigaagent/` с разными командами запуска.

1. Перенесите COMMON.example.yaml в COMMON.yaml конфигурационного репозитория.
2. Перенесите agents/integrations/namespace примеры в пути, ожидаемые вашей
   версией pipeline для STAND=IFT. Имя stand.yaml в инструкции является примером;
   сверяйте также namespace/ против namespaces/ с установленным шаблоном.
3. Замените все `<...>` реальными IDs, DNS, ports, credentials ID и именами
   Kubernetes Service. Укажите все Kafka brokers и при необходимости отдельные
   telemetry clusters, IDP OCR route, сертификаты и платформенную наблюдаемость.
   YAML-примеры задают функциональный минимум, не полный набор инфраструктуры
   любого namespace. Формат COMMON/AGENT_CUSTOM_ARGS взят из инструкции v6.
4. Смонтируйте три файла SecMan. Для ядра/AIFA в этой папке расширенные шаблоны
   с ключом шифрования чатов. Redis в этой раскатке не используется. Workstation использует свой шаблон.
5. Выполните платформенный Liquibase-шаг из `aifa/desc.yaml`:
   `aifa/liquibase/changelog.sql` создаёт структуру AIFA и GigaAgent.
   Используйте отдельную DDL-роль; шаг должен успешно завершиться до запуска pod.
6. Pod всех ролей GigaAgent и AIFA работают без DDL-прав. GigaAgent проверяет
   `gigaagent_schema_version` только чтением. Alembic и миграционные команды
   из образа GigaAgent удалены. Подключения обоих сервисов должны указывать
   на схему из changelog; запуск на пустой схеме завершится понятной ошибкой.
7. Примените CD через jenkins/CD/cd_customResources.groovy, передав CONFIG_REPO,
   CONFIG_BRANCH, DISTRIB, STAND и поддерживаемую template_version. В новом
   namespace updateRoleBindings=true. Workstation и AIFA инициализируют свои
   схемы отдельно от ядра.

Agent-runtime загружает доверенные расширения тем же загрузчиком, что server.py.
В поставке aifa-engine регистрирует 9 инструментов. Ошибка загрузки останавливает
startup; частичная регистрация очищается. При штатном завершении runtime сначала
дожидается своих задач, выгружает расширения и затем закрывает общие клиенты.
При незавершённом drain ресурсы сохраняются для ещё работающих задач.

В AIFA FINENGINE_OUROBOROS_URL ведёт на transport:9090; agent-runtime:9090
обслуживает probes. AIFA и GigaAgent используют один секрет создания Workstation.
Не публикуйте transport/runtime/станцию напрямую пользователям. Профиль IFT:
по одной реплике, 4 слота runtime, без фронта/helper и без дистилляции памяти.

Проверки в репозитории: настоящий loader + compose_process, каталог из 9
инструментов, HTTP route AIFA для всех 9 инструментов с двумя владельцами,
ошибка авторизации, отказ загрузки повреждённого плагина, отзыв доверия и unload,
согласованность COMMON/agents и shell-поведение entrypoint.
Бизнес-обработка AIFA в HTTP-тесте заменена записывающим обработчиком; это
проверка регистрации/доставки контекста, а не расчёта отчёта через реальную модель.
Полная приёмка пяти процессов с Kafka/PostgreSQL/S3, выполнение Linux
песочницы и Operator требуют стенда контура. Здесь они не запускались.

БД, таблицы, сетевая матрица и параметры: `../../../aifa/deploy/MU-DEPLOYMENT-GUIDE.md`.

Результаты и границы локальной проверки: [TEST-RESULTS.md](TEST-RESULTS.md).

## Профиль без Redis

У пяти компонентов ядра OUROBOROS_BROKER_BACKEND=kafka и
OUROBOROS_SETTINGS_CACHE_BACKEND=none. Настройки читаются из основного хранилища
без Redis-кеша. Redis/Radish интеграция и секреты URL удалены из шаблонов.

AIFA: одна реплика и один worker, FINENGINE_REDIS_URL пуст. Используется
локальная шина внутри процесса. При таком профиле не увеличивайте число её
реплик/workers без отдельного решения об общей шине. Workstation REDIS_ENABLED=false.
Если Redis URL уже был записан в настоящие SecMan-файлы, удалите его оттуда:
непустой FINENGINE_REDIS_URL из SecMan перекрывает пустой ENV.

MU продолжает работать на Kafka; число постоянных прикладных pod остаётся 7.

## Рабочий каталог на платформе

В корпоративном окружении запись разрешена в `/tmp`:

```yaml
OUROBOROS_APP_ROOT: /tmp
OUROBOROS_DATA_DIR: /tmp/data
OUROBOROS_REPO_DIR: /tmp/repo
```

Entrypoint копирует содержимое образа из `/app` в `/tmp/repo` при запуске,
до миграций и загрузки навыков. `/app` используется только для чтения.
Это работает и при отдельном временном томе на `/tmp`.
Файл порта создаётся в `/tmp/data/state/server_port`, PID — в `/tmp/ouroboros.pid`.
Эти настройки нужно проверять и в итоговом Deployment после подстановок Jenkins/AEF.
Старые явные значения `OUROBOROS_PORT_FILE`, `OUROBOROS_PID_FILE`,
`OUROBOROS_SETTINGS_PATH` или `OUROBOROS_DRIVE_ROOT` не должны направлять запись
в `/data` или `/app`. Локальные файлы в `/tmp` временные;
PostgreSQL и S3 настраиваются отдельно.

Пароль в платформенном PostgreSQL TOML URL-кодирован: в корпоративном desc
и примерах задан `SECMAN_POSTGRES_PASSWORD_ENCODING=url`. Загрузчик декодирует
поле `password` один раз перед сборкой DSN. Для исходного пароля используйте
`raw` (умолчание). Flat JSON/dotenv DSN этим флагом не изменяются.
