# ADR-0052. Запрос подзадачи уходит supervisor'у в конце вызова `schedule_task`

Статус: предложено (шаг 16; основание — ADR-0046 п. B, решение владельца R31 «до шага 16 — правка ядра» и ответ владельца 20.10.2026 «правка ядра в этой сессии»)
Исполняет: ADR-0046 (вариант B). Исключение из: ADR-0019 (ядро не правим), F1

## Контекст
ADR-0046 показал на живом ouroboros: `_schedule_task` кладёт `SubtaskRequested` в `ctx.pending_events`
(`ouroboros/tools/service/orchestration.py`), а supervisor получает список только как результат
завершённого run (`ouroboros/agent/core.py`, `return list(self._pending_events)`; публикация —
`ouroboros/agent_runtime/executor.py`, `_run_task`). Подзадача поэтому стартует после корня, и
`aifa_wait(task_id)` из корня (ADR-0041 п. 3, ADR-0043 п. 3) ждёт задачу, которая не стартует до его конца.

Факты, на которых держится минимальная правка:
- В ядре есть прецедент «не копить до конца run»: `EventSinkAdapter.ask_owner`
  (`ouroboros/agent/loop_adapters.py`) публикует через `IAgentEventPublisher` сразу и объясняет это
  комментарием «not a pending event: those flush when the task ends».
- Обработчик supervisor'а (`supervisor/events.py`, `_handle_schedule_task` → `enqueue_task`) от момента
  публикации не зависит; подзадачи (`parent_task_id` не пуст) не участвуют в потолке корней владельца и
  в сериализации корней чата (`ouroboros/storage/dialect.py`, `root_claim_sql`), поэтому могут идти рядом
  с работающим родителем при `OUROBOROS_WORKER_SLOTS ≥ 2`.
- Остановка родителя уже учитывает запрос, принятый после неё: `openspec/specs/task-lifecycle/spec.md`
  («Child persistence loses the terminal race», «A completed parent's scheduling event is still pending»)
  и ответ supervisor'а «not scheduled because its parent was stopped».

## Решение
1. Новый необязательный метод `ToolEvents.request_subtask(request) -> bool` (`new_core/tools/interfaces.py`):
   отдать сохранённый запрос подзадачи supervisor'у сейчас. По умолчанию `False` («некому отдать»);
   `PublishedToolEvents` (`ouroboros/tools/events.py`) публикует запрос через `IAgentEventPublisher` и
   возвращает `True`, при сбое транспорта пишет предупреждение и возвращает `False`.
2. `_schedule_task`: после успешной записи строки `requested` (запись — раньше публикации, иначе
   обработчик supervisor'а мог бы опередить её) для задачи, которая **не** durable-job, вызывает
   `ctx.events.request_subtask(requested)`; при `True` запрос убирается из `ctx.pending_events` (второй
   доставки нет — доставка «не более одного раза», `agent-runtime-events`). При `False`, при сбое записи
   и для durable-job путь прежний: запрос уходит с результатом run.
3. Остальное не меняется: пределы подзадач, отмена (`cancel_task`), снятие запросов при отмене корня
   (`_settle_cancelled_child_requests` остаётся для запросов, оставшихся в `pending_events`).
4. ADR-0041 и ADR-0043 работают как написаны; контрольный тест
   `test_c2_4_ouroboros_starts_a_subtask_only_after_its_root_has_finished` (ADR-0046, «Следствия»)
   инвертирован: подзадача стартует до конца корня.

## Следствия
- Расхождение с базой `base/ouroboros-1.5.1` — три файла кода (`new_core/tools/interfaces.py`,
  `ouroboros/tools/events.py`, `ouroboros/tools/service/orchestration.py`), тесты набора ouroboros и
  OpenSpec-изменение `openspec/changes/subtask-request-during-parent-run/`; каждый путь — строка
  `путь ADR-0052` в `core_freeze_exceptions.txt` (F1). `uv.lock` не меняется (F11).
- Подзадача может занять слот работника, пока корень ждёт её в `aifa_wait`; при одном слоте корень
  ждал бы до срока (600 с) — F9 держит `OUROBOROS_WORKER_SLOTS ≥ 2` (C4.5).
- Правка ядра — вторая после ADR-0038; при обновлении базы ouroboros перенести или заменить штатной
  возможностью ядра.
- Независимое ревью правки (Definition of Done ouroboros) — роль ревью шага 16.

## Чем проверяется
Тесты набора ouroboros (`tests/ouroboros/tools/test_tool_events.py`,
`tests/ouroboros/agent_events/test_task_status_flow.py`); контрактный C2.4 на живом ouroboros; F1 (исключения).
