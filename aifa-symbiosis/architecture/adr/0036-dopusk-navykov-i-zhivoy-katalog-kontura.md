# ADR-0036. Допуск навыков владельцам; живой каталог инструментов контура; секрет Workstation

Статус: принято (владелец, 16.10.2026; предложено шагом 04, 15.10.2026)
Уточняет: ADR-0027 (набор настроек), ADR-0028 (п. 3, секрет Workstation)

## Контекст
Шаг 04 поднял `aifa-sym` на dev и проверил живой экземпляр (ADR-0027: «точный перечень
фиксируется на этапе 0»).

- Агент в `OUROBOROS_MULTIUSER=true` не видел `code_exec`: активными были только 11 core-инструментов.
  Причина — допуск навыков. Встроенные `workstation_connect`, `code_exec`, `file_read`, `file_write`,
  `file_edit`, `apply_patch` принадлежат навыку `workstation`
  (`config_registry/user_settings/tool_access.py:20`); в многопользовательском режиме без допущенного
  навыка они отказывают `TOOL_UNAVAILABLE` (`tool_access.py:53`). Допуск задаёт
  `OUROBOROS_USER_ENABLED_SKILL_IDS` — переменная окружения и allowlist владельца, по умолчанию
  `[]` (`docs/SETTINGS.md` §5; `ouroboros/agent/task_graph.py:88`). В наборе ADR-0027 её нет.
- Живой каталог (`BuiltinToolProvider`, процесс контейнера, 15.10.2026): 48 встроенных инструментов —
  12 core, 9 discoverable, 27 excluded; журнал сервера: `Tool catalog initialized tools=47` (разница в
  один не выяснялась). Навыков поставки 8; отключено 7 (все, кроме `workstation`).
- Живая проверка P2: `ctx.get_settings` / `env_from_settings` отдают расширению только ключи схемы
  `AppSettings`; `AIFA_ENGINE_URL` и `AIFA_ENGINE_TOKEN` в нём нет (`key in settings` — `False`),
  в `os.environ` они есть (`ouroboros/skills/extensions/loader.py:722`, `server.py:350`).
- Workstation контура (`workstation-svc:local`, образ dev) секрет создания сессии не проверяет: в
  `app/core/config.py` нет такой настройки, заголовка `X-Session-Create-Secret` в `app/` нет.

## Решение
1. В набор ADR-0027 добавляется `OUROBOROS_USER_ENABLED_SKILL_IDS=distribution:workstation`
   (значение — канонический `source_id:local_id`). Навыки `aifa-*` типа `instruction` добавляются
   этим же списком новой записью, когда они появятся; исполняемые (`script`, `tool`, `extension`) —
   см. ADR-0037.
2. Списки `OUROBOROS_DISABLED_TOOLS` (27 имён) и `OUROBOROS_DISABLED_SKILLS` (7) подтверждены живым
   каталогом: каждое имя — встроенный инструмент/навык поставки, лишних и недостающих нет. Снимок F9
   не меняется по этим двум спискам.
3. Адрес службы и токен плагин `aifa-engine` читает из `os.environ` (запасной вариант ADR-0027,
   «Следствия»); `read_settings` для них не используется.
4. ADR-0028 п. 3 («Workstation контура требует `WORKSTATION_CREATE_SECRET`») на этой сборке
   Workstation не выполняется. Защита контура — внутренняя сеть без опубликованных портов
   (ADR-0028 п. 1). Переменная в окружении Workstation оставлена: клиент ouroboros присылает
   заголовок, сервис его игнорирует; когда сервис начнёт проверять — защита включится без правки
   контура. Секрет не считается проверенным и не упоминается как мера защиты.

## Следствия
- Снимок F9 (`dev_config.snapshot.json`) получает список `OUROBOROS_USER_ENABLED_SKILL_IDS`.
- Допуск — instance-значение: он действует для всех владельцев (пользователей AIFA); индивидуальный
  допуск по `PUT /api/owner-settings/...` не нужен.
- Пока ADR не принята, значение стоит в `ouroboros.env` на dev и в `ouroboros.env.example`
  (F9 держит их равными снимку).

## Чем проверяется
F9 (`test_dev_config.py`: значение списка и нарушитель «допуск убран»); дымовая 3
(`aifa/deploy/dev/smoke/run.sh 3`) — `code_exec` выполняется в Workstation контура.
