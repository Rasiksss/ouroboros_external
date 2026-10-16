# ADR-0027. Ouroboros настраивается только окружением; обязательный набор настроек

Статус: принято (14.10.2026, сессия архитектуры); `schedule_task` в списке отключённых заменён ADR-0043

## Контекст
Ядро ouroboros не меняем (`SYMBIOSIS.md` §1). Всё, что нужно AIFA от ouroboros, задаётся
переменными окружения (`ouroboros/storage/settings/schema.py`, `docs/SETTINGS.md`).

## Решение
Обязательный набор (значения — в `aifa/deploy/dev/ouroboros.env.example`, секреты — вне git):

| Настройка | Значение | Почему |
|---|---|---|
| `OUROBOROS_MULTIUSER` | `true` | личность из доверенных заголовков (ADR-0014) |
| `BROWSER_INGRESS_ENABLED` | `true` | WebSocket и HTTP браузерного транспорта (ADR-0021) |
| `OUROBOROS_WORKER_SLOTS` | `4` (≥ 2) | корень + автор на пару пользователей (ADR-0008) |
| `OUROBOROS_HARD_TIMEOUT_SEC` | `2700` | стандартный путь + цикл 1200 с + автор с запасом (ADR-0023) |
| `OUROBOROS_MEMORY_DISTILL` | `false` | ADR-0010 |
| `OUROBOROS_TOOL_CONFIRMATION_ENABLED` | выключено | ADR-0010 |
| `OUROBOROS_ENABLED_TOOLS` | восемь `aifa-engine.*` | в первый запрос (ADR-0022) |
| `OUROBOROS_DISABLED_TOOLS` | чтение файлов и вложений, веб, расписания, память, картинки, `send_*` | ADR-0016, ADR-0025 |
| `OUROBOROS_DISABLED_SKILLS` | все навыки поставки, кроме `aifa-*` и `workstation` | ADR-0016 |
| `WORKSTATION_BASE_URL` | Workstation контура | ADR-0007 |
| модели агента | `GLM-5.2` через Cloud.ru | ADR-0017 |
| `A2A_*`, `DIALOGS_*` | не заданы | ADR-0016 |

Точный перечень отключаемых инструментов фиксируется на этапе 0 по `list_available_tools` живого
экземпляра; он входит в снимок F9.

## Следствия
- Изменение набора — новая запись ADR и обновление снимка F9.
- Адрес службы и секрет внутреннего API плагин получает через `env_from_settings`/`read_settings`,
  если загрузчик расширений отдаёт ключи вне схемы настроек; иначе — `os.environ` в `plugin.py`
  (доверенный код поставки). Выбор подтверждается на этапе 0.

## Чем проверяется
F9.
