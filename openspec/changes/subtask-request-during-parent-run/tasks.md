## 1. Failing tests first

- [x] 1.1 `tests/ouroboros/tools/test_tool_events.py`: `request_subtask` publishes and returns `True`; no publisher and a refusing transport return `False`; the null events return `False`
- [x] 1.2 `tests/ouroboros/agent_events/test_task_status_flow.py`: `_schedule_task` hands the request over after the `requested` row exists and leaves `pending_events` empty; without a publisher the request stays pending; a durable job still reports through the run result
- [x] 1.3 Run them against the unchanged code and record that they fail

## 2. Interface and adapter

- [x] 2.1 `ToolEvents.request_subtask` with the `False` default
- [x] 2.2 `PublishedToolEvents.request_subtask`

## 3. Schedule

- [x] 3.1 `_schedule_task` hands the request over after a successful record write for a run that is not a durable job
