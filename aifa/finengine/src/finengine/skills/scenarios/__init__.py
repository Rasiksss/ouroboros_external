"""Scenarios — user-defined scheduled tasks («Сценарии»).

A *scenario* is a server-side, schedulable unit of work owned by a user:
run a built-in skill (IR Calendar / Daily News) or a free-form agent prompt,
either manually or on a recurring schedule. Output is persisted as a normal
project artifact under the scenario's id namespace, so the scenario's run
history is just `artifacts.list_artifacts(user_id, scenario_id)`.

Modules:
  store      — SQLite persistence (data/scenarios.db) + CRUD
  runner     — run_scenario(): dispatch by kind, save artifact, set status
  scheduler  — in-process asyncio loop firing due scenarios (MSK)
"""
