---
name: tasks-scheduler
description: "Time-based reminders and agent tasks via create_reminder, create_scheduled_task, and confirm_schedule. Never use schedule_task or send_user_message as fallback."
version: 2.0.0
type: instruction
when_to_use: "The user asks for a reminder or task tied to a future time, or asks to list, change, pause, resume, cancel, confirm, or move an existing schedule to the current chat."
---

# Tasks Scheduler

Use the scheduling tools for future or recurring work. Do not use them for a subtask that should run now.

They are not in your active tool set until you ask for them. Call
`enable_tools(tools='create_reminder,create_scheduled_task,confirm_schedule,list_schedules,edit_schedule,cancel_schedule,pause_schedule,resume_schedule,move_schedule_here')`
first — enable them all at once, because every flow below needs at least two of them.

## Hard boundary

Never substitute `schedule_task`, `send_user_message`, or another generic tool for the
scheduler tools. Those tools run immediately and do not create a durable
time-based schedule.

If `enable_tools` does not return them, say the scheduler is unavailable here. In that state, do not
claim that anything was scheduled, deduplicated, executed, or delivered.

A one-time reminder is active when `create_reminder` reports `state=active` and a future
`next_run_at`. Recurring reminders and all agent tasks are active only after a successful `confirm_schedule`.
A completed generic subtask or immediate message send is not evidence
that a reminder was scheduled for the requested time.

## Workflow

1. Use `create_reminder` when delivery is just the saved message; it does not run an LLM.
2. Use `create_scheduled_task` when the future run must reason or call tools, such as checking mail.
3. Read back `what`, `summary`, state, next run, and delivery information.
4. For recurring reminders and all agent tasks, call `confirm_schedule` only after the user confirms. Pass the returned `id` and `revision`.
5. Use `list_schedules` before later mutations to obtain the current revision. It answers one page of this chat's schedules, soonest first: narrow it with `state`, and pass `cursor=next_cursor` while `next_cursor` is present to reach the rest. A schedule belongs to the chat it was created in, so one created elsewhere is neither listed nor reachable here.

Make `what` self-contained. Name concrete projects, identifiers, periods, and expected output. The run happens later — do not rely on anything from the conversation that created it still being in view.

Use `move_schedule_here` when a schedule of this chat should now reach whoever is asking rather than the participant who created it. It re-addresses delivery inside this chat and cannot bring a schedule in from another one. Never ask the model or user to invent a raw chat id.

Resume skips missed slots and schedules the next future occurrence. A one-time timestamp that has passed cannot be confirmed.

Dispatch is at-most-once per claimed slot. A crash after claiming but before injection can leave a run requiring attention; do not retry it blindly because the external side effect may already have happened.
