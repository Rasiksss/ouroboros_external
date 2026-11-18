# Локальные проверки MU + AIFA

Дата: 05.11.2026. Платформа: Windows, Python 3.12.

| Набор | Результат |
|---|---|
| gigaagent/tests (SecMan, MU extensions, deployment/entrypoint) | 27 passed |
| aifa/finengine/tests/test_secman.py | 7 passed |
| Workstation: unit/test_secrets + session-secret enforcement + clients integration | 14 passed |
| Итого целевых проверок | 48 passed |

Что проверено:

- compose_process отдельного agent-runtime использует настоящий ExtensionRuntime;
- настоящий distribution loader импортирует aifa-engine/plugin.py и регистрирует 9 инструментов;
- все 9 инструментов вызываются через ToolCatalog и HTTP router AIFA;
- параллельные alice/bob сохраняют раздельные owner/chat/task/run идентификаторы;
- токен из SecMan применяется к запросу, неверный токен не достигает обработчика;
- неподтверждённое расширение не загружается, отзыв доверия выгружает инструменты;
- ошибка register(api) останавливает startup и очищает частичную регистрацию;
- unload очищает живой каталог;
- COMMON и agents согласованы по компонентам, build paths и Python-командам;
- рабочие MU pod отключают миграции; default, skip, неверный режим и ошибка
  миграции проверены запуском entrypoint в shell с тестовой командой Python;
- SecMan и API совместимость Workstation с GigaAgent/AIFA остаются рабочими.

Дополнительно: проверены YAML всех четырёх Operator-примеров, наличие COPY-источников
корпоративного Dockerfile, shell/Python syntax и lint изменённых Python-файлов.

Граница проверки: в compose_process тесте инфраструктурный DB engine заменён
тестовым SQLite engine; реальный PostgreSQL не подключался. В HTTP-тесте используется
настоящий router/auth/контракт AIFA и записывающий обработчик вместо бизнес-расчёта.
Kafka/Redis/S3 и модель не запускались. Docker daemon недоступен; корпоративная
сборка образов, пять процессов через реальные брокеры, Operator и выполнение
Linux Workstation sandbox здесь не проверены. Тесты не заменяют приёмку в контуре.
