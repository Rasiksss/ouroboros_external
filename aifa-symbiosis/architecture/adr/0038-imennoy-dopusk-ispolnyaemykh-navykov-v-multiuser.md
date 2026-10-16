# ADR-0038. Именной допуск доверенных исполняемых навыков поставки в многопользовательском режиме

Статус: принято (владелец, 16.10.2026; предложено шагом 04; форма решения ADR-0037: вариант A, узкая форма, минимальная правка ядра)
Исполняет: ADR-0037. Исключение из: ADR-0019 (ядро не правим), F1

## Контекст
ADR-0037 принят с вариантом A: исполняемые навыки поставки должны быть видны в
`OUROBOROS_MULTIUSER=true`. Требование ouroboros `openspec/specs/multiuser-mode/spec.md` («No skill that
executes code is exposed in the mode») — сознательный потолок безопасности: ни список владельца, ни
задача его не расширяют. Широкая форма («все навыки поставки видны») сняла бы потолок для любого будущего
навыка в `skills/`; владелец выбрал узкую форму и минимальные правки ядра (16.10.2026).

## Решение
1. Новая настройка развёртывания `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS` — список канонических id
   `distribution:<навык>`, по умолчанию пуст. Id другого источника, пустой элемент или не список —
   ошибка загрузки настроек.
2. Общая поверхность навыков в многопользовательском режиме скрывает исполняемые навыки, кроме названных
   в этом списке: `visible = executable_skills OR NOT is_executable() OR id ∈ trusted`. Один предикат в
   общей поверхности, без правил в провайдерах.
3. Допуск к задаче не меняется: названный навык попадает в задачу, только если его называет и
   `OUROBOROS_USER_ENABLED_SKILL_IDS` (instance-значение или владелец/агент).
4. Исполнитель навыков (`skill_exec`) в режиме по-прежнему не регистрируется: рабочий исполняемый тип —
   `extension`. Для `aifa-engine` (ADR-0022) список содержит `distribution:aifa-engine`.
5. Правка ядра — три файла кода: `ouroboros/storage/settings/schema.py` (поле и валидатор),
   `new_core/skills/surface.py` (`SkillAvailability.trusted_executable_skills`, `_is_visible`),
   `ouroboros/skills/availability.py` (передача значения); плюс `docs/SETTINGS.md`, OpenSpec-изменение
   `openspec/changes/trusted-distribution-executable-skills/` и тесты набора ouroboros. Поведение при
   пустом списке и вне режима не меняется.

## Следствия
- F1: каждый изменённый файл ядра — строка `путь ADR-0038` в `core_freeze_exceptions.txt` (11 путей).
  Расхождение с базой `base/ouroboros-1.5.1` — только эти файлы; `uv.lock` не меняется (F11).
- Набор настроек ADR-0027 дополняется `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS=distribution:aifa-engine`
  (снимок F9). Пока навыка `aifa-engine` нет (шаг 10), значение ничего не открывает.
- `OUROBOROS_USER_ENABLED_SKILL_IDS` получит `distribution:aifa-engine` на шаге 10 новой записью ADR-0036/0027.
- Доверенное расширение работает в процессе сервера с его правами; список — единственная точка доверия
  развёртывания. Под режимом расширению недоступен `send_message` (нет автора): `aifa-engine` им не пользуется.
- Живая проверка именно `extension` — на шаге 10: в контуре сейчас нет ни одного `extension`-навыка.
- При следующем обновлении базы ouroboros правку перенести или заменить штатной возможностью ядра.

## Чем проверяется
Тесты набора ouroboros (`tests/new_core/skills/test_skill_surface.py`,
`tests/ouroboros/storage/settings/test_trusted_executable_skill_ids.py`); F1 (исключения), F9 (значение
в снимке), F13 (ADR вместе со снимком).
