# ADR-0054. Автор атомов с кодом — навык подзадачи; автор службы удаляется

Статус: предложено (шаг 17)
Исполняет: ADR-0029 (этап 4), ADR-0041 п. 5, ADR-0043 пп. 6, 9, ADR-0044 п. 7
Уточняет: ADR-0036 (допуск навыков), ADR-0042 п. 1 (`aifa_move`: атом с кодом в `new_atoms`)

## Контекст
- До шага 17 код атома писал автор внутри службы: `atom_author.py` с промптом `atom_author.md` и моделью автора
  (`Settings.atom_author_model`, `atom_author_model_timeout_seconds`), вызываемый `LoopSession._ensure_atom` при
  атоме с кодом в `new_atoms`. Шаг 16 дал подзадачам общие операции (`aifa_register_atom`, `aifa_report`,
  `aifa_wait(task_id)`) и общую проверку кода `atom_intake`; автор службы уже сдаёт код через неё (ADR-0044 п. 1).
- ADR-0041: служба навыков не знает; навык подзадачи выбирает планировщик; новый навык = `skills/aifa-<имя>/SKILL.md`
  и его id в `OUROBOROS_USER_ENABLED_SKILL_IDS` (ADR-0036, снимок F9).
- ADR-0043 п. 6, п. 9 и ADR-0044 п. 7 уже называют итог: подсказки цикла ведут к подзадаче и `aifa_register_atom`,
  атомы с кодом не появляются в `new_atoms` `dynamic_move`.
- Правила кода автора лежат в `atom_author.md` (он главнее плана 34 §7.1: файлы из `out/`, библиотеки комнаты,
  `openpyxl`, `python-pptx`, `python-docx`).
- Из `atom_author.py` ещё нужны `LoopSession` и `DynamicRun`: `case_inputs` (реальные входы атома из `Case`, ими
  кормится проверка `atom_intake`), `end_of_chain` (таблица артефактов), `without_markdown_fence` (ответ модели
  планировщика службы).

## Решение
1. **Навык `aifa-atom-author`** (`skills/aifa-atom-author/SKILL.md`, `type: instruction`), id
   `distribution:aifa-atom-author`. `description` (≤ 260 знаков) говорит, когда планировщику его звать: нужен атом с
   кодом, которого нет в каталоге. Порядок: задание → входы `aifa_state(artifact_ids)` (и `aifa_doc`) → код →
   отладка в своей сессии Workstation (`file_write`, `code_exec`) → `aifa_report` `ok`/`error` после каждого прогона →
   `aifa_register_atom`; на `ОТКАЗ` исправляется причина → `aifa_report(status="done")`. Правила кода — из
   `atom_author.md` без изменений по существу; навык не обещает того, чего не держит комната (сети, окружения и
   сторонних библиотек в ней нет, ADR-0007), пределы регистрации называет как ответы службы.
2. **Допуск.** `distribution:aifa-atom-author` добавлен в `OUROBOROS_USER_ENABLED_SKILL_IDS`
   (`ouroboros.env.example`, снимок `dev_config.snapshot.json`, профиль `contract`; F9). Это значение
   `ouroboros.env` на dev при выкатке шага 17.
3. **Раздел «Делегирование»** навыка `aifa-planning`: роль выбирается по каталогу навыков, задание
   `schedule_task(description="Навык: <id>. …")`, ожидание `aifa_wait(task_id)`, по итогу — продолжить план;
   `ОТКАЗ подзадача_не_ответила` → `get_task_result`; висящую подзадачу снимает `cancel_task`; перед `aifa_finish`
   сняты все незавершённые (ADR-0043 п. 8). Новый атом с кодом — только через подзадачу.
4. **`aifa_move`.** Атом с кодом в `new_atoms` — `ОТКАЗ неверные_аргументы`: «атом с кодом заводит подзадача: поручи её
   навыку автора, она сдаст код `aifa_register_atom`». Атомы-загрузки (`url`) остаются в `new_atoms`. Подсказки цикла
   («входов ещё нет», «пришли его описание с тем же name», обещанный файл не собран — `_with_promised_files`) ведут к
   подзадаче и `aifa_register_atom`, а не к `new_atoms`.
5. **Удаляются** `skills/spec/atom_author.py`, `atom_author.md`, поля `Settings` `atom_author_model` и
   `atom_author_model_timeout_seconds` (`FINENGINE_ATOM_AUTHOR_MODEL*` в окружении службы, compose и снимках),
   параметр `author` у `LoopSession` и `DynamicRun`. `case_inputs`, `end_of_chain` и `without_markdown_fence` переносятся
   без изменения поведения в `skills/spec/atom_inputs.py` отдельным структурным коммитом.
6. **Следствие для временного ведущего.** При `loop_driver=engine` (переключатель удаляется в шаге 19) модель службы
   не может создать код атома: её `new_atoms` с кодом получают тот же отказ. Путь по умолчанию — `ouroboros`.

## Следствия
- В службе нет модели автора и ни одной ветки по имени навыка (C4.7, F2); код новых атомов на `aifa-sym` пишет
  подзадача ouroboros, запущенная планировщиком; лента «Планировщик и автор» показывает попытки как раньше
  (`author_attempt` из `aifa_report` и `aifa_register_atom`, ADR-0044 п. 1).
- Прежний код принятого атома подзадача не видит (исходники временных атомов в таблицу артефактов не входят): исправление
  — новое задание с тем же именем, где сказано, что неверно.
- Расход токенов автора уходит из учёта службы (`usage` службы) в расход ouroboros (ADR-0047 п. 3: только расход службы).

## Чем проверяется
F9 (допуск в примере, профиле и снимке); F2 (нет ссылок на удалённое, навык без кода); C4.1–C4.9 на настоящем
ouroboros; реплей записанных ответов автора через корень и подзадачу (`aifa/tests/contract/test_atom_author_replay.py`);
`CONTRACTS.md` §3.1 (`aifa_move`), §4.
