---
name: tasks-scheduler
description: "Time-based reminders and agent tasks via create_reminder, create_scheduled_task, and confirm_schedule. Never use schedule_task or send_user_message as fallback. If scheduler tools are absent, report that the extension is not live; do not claim anything was scheduled or delivered."
version: 1.1.0
type: extension
runtime: python3
entry: plugin.py
permissions: [tool, fs, read_settings, supervised_task, inject_chat, send_message]
env_from_settings:
  - OUROBOROS_TIMEZONE
when_to_use: "The user asks for a reminder or task tied to a future time, or asks to list, change, pause, resume, cancel, confirm, or move an existing schedule to the current chat."
---

# Tasks Scheduler

Use the registered scheduling tools for future or recurring work. Do not use them for a subtask that should run now.

## Hard boundary

Never substitute `schedule_task`, `send_user_message`, or another generic tool for the
registered scheduler tools. Those tools run immediately and do not create a durable
time-based schedule.

If `create_reminder`, `create_scheduled_task`, `confirm_schedule`, or the other scheduler tools are unavailable,
say that the tasks-scheduler extension is not live. In that state,
do not claim that a schedule was created, deduplicated, executed, or delivered.

A one-time reminder is active when `create_reminder` reports `state=active` and a future
`next_run_at`. Recurring reminders and all agent tasks are active only after a successful `confirm_schedule`.
A completed generic subtask or immediate message send is not evidence
that a reminder was scheduled for the requested time.

## Workflow

1. Use `create_reminder` when delivery is just the saved message; it does not run an LLM.
2. Use `create_scheduled_task` when the future run must reason or call tools, such as checking mail.
3. Read back `what`, `summary`, state, next run, and delivery information.
4. For recurring reminders and all agent tasks, call `confirm_schedule` only after the user confirms. Pass the returned `id` and `revision`.
5. Use `list_schedules` before later mutations to obtain the current revision.

Make `what` self-contained. Name concrete projects, identifiers, periods, and expected output. A scheduled run has no access to the conversation that created it.

Use `move_schedule_here` when the user says that an existing schedule should now deliver in the current chat. Never ask the model or user to invent a raw chat id.

Resume skips missed slots and schedules the next future occurrence. A one-time timestamp that has passed cannot be confirmed.

Dispatch is at-most-once per claimed slot. A crash after claiming but before injection can leave a run requiring attention; do not retry it blindly because the external side effect may already have happened.
