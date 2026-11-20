"""Per-task context policy; stored history remains intact."""

from __future__ import annotations

HISTORY_TOOLS = frozenset({
    "chat_history", "recent_tasks", "memory_read", "memory_list", "memory_save", "memory_delete",
    "update_scratchpad", "data_read", "data_list",
})
MEMORY_SECTIONS = frozenset({"memory", "knowledge", "scratchpad", "recalled_memories", "recent_chat", "working_retrieval"})


def memoryless_for_task(task: dict) -> bool:
    if "memoryless_mode" in task:
        return bool(task["memoryless_mode"])
    from ouroboros.storage.settings.store import load_settings

    return load_settings().OUROBOROS_MEMORYLESS_MODE


def _tasks():
    from ouroboros.storage.runtime_persistence import runtime_persistence

    return runtime_persistence.current.tasks_repo


def _in_current_tree(ctx, task_id: str) -> bool:
    root = str(ctx.root_task_id or ctx.task_id or "")
    if task_id in {root, str(ctx.task_id or "")}:
        return True
    seen = set()
    store = _tasks()
    while task_id and task_id not in seen:
        seen.add(task_id)
        task = store.get_task(task_id)
        if task is None or (task.owner_id, task.agent_id) != (ctx.scope.owner_id, ctx.scope.agent_id):
            return False
        task_id = str(task.parent_task_id or "")
        if task_id == root:
            return True
    return False


def tool_refusal(ctx, name: str, args: dict) -> str | None:
    if not getattr(ctx, "memoryless_mode", False):
        return None
    blocked = name in HISTORY_TOOLS
    target = str(args.get("parent_task_id" if name == "schedule_task" else "task_id") or "").strip()
    if name in {"get_task_result", "wait_for_task", "cancel_task"} or (name == "schedule_task" and target):
        blocked = not target or not _in_current_tree(ctx, target)
    if blocked:
        return (f"⚠️ TOOL_MEMORYLESS_MODE ({name}): history and previous tasks are unavailable for this run. "
                "Use the current request, its attachments and current subtasks; ask the user for missing input.")
    return None
