"""Общее для тестов навыка aifa-engine: загрузка `plugin.py` по пути и запись регистраций.

Каталоги тестов не пакеты, поэтому имя модуля уникально среди `skills/*/tests` (pytest не терпит
двух одноимённых файлов без `__init__.py`).
"""
# ruff: noqa: RUF003

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from new_core.identity.owner_scope import OwnerScope
from new_core.tools import ToolContext, ToolSchema
from ouroboros.skills.extensions.loader import extension_surface_name

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[1]
SNAPSHOT = REPO_ROOT / "aifa" / "contracts" / "tools.snapshot.json"
SKILL_NAME = "aifa-engine"

RUN_ID = "run-11111111"
ROOT_TASK = "root-task"


def load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aifa_engine_plugin", SKILL_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # датаклассам с отложенными аннотациями нужен модуль в sys.modules
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Registered:
    """Одна регистрация `PluginAPI.register_tool`: обработчик и всё, что к нему приложено."""

    name: str
    handler: Any
    description: str
    schema: dict[str, Any]
    options: dict[str, Any]


class RecordingApi:
    """Подставной `PluginAPI`: запоминает регистрации, как их увидит загрузчик расширений."""

    def __init__(self) -> None:
        self.tools: dict[str, Registered] = {}

    def register_tool(
        self, name: str, handler: Any, *, description: str, schema: dict[str, Any], **options: Any
    ) -> None:
        assert name not in self.tools, f"{name} зарегистрирован дважды"
        self.tools[name] = Registered(name, handler, description, schema, options)


def registered_tools() -> dict[str, Registered]:
    api = RecordingApi()
    load_plugin().register(api)
    return api.tools


def schema_chars(tool: Registered) -> int:
    """Знаки схемы так, как их считает `ouroboros/agent/task_graph.py` (`_initial_tool_schemas`).

    Тот же вызов `json.dumps(..., ensure_ascii=False, default=str)` над `ToolSchema` с именем,
    которое присваивает загрузчик расширений: схема-датакласс json не знает, и считается её
    `str(...)`, поэтому длина — не длина «чистого» JSON.
    """
    schema = ToolSchema(
        name=extension_surface_name(SKILL_NAME, tool.name),
        description=tool.description,
        parameters=tool.schema,
    )
    return len(json.dumps(schema, ensure_ascii=False, default=str))


def tool_context(*, stop_after: int | None = None, **fields: Any) -> ToolContext:
    """Настоящий `ToolContext` корня хода; `stop_after` — через сколько опросов «Стоп» станет истиной."""
    values: dict[str, Any] = {
        "repo_dir": REPO_ROOT,
        "drive_root": REPO_ROOT,
        "scope": OwnerScope(owner_id="7", agent_id="main", surface_id="browser", chat_id="project-1"),
        "skill_surface": None,
        "task_id": ROOT_TASK,
        "root_task_id": None,
        "task_depth": 0,
        "trace_id": "0a0a0a0a-0a0a-4a0a-8a0a-0a0a0a0a0a0a",
        "transport": {"source_message_id": RUN_ID},
    }
    values.update(fields)
    ctx = ToolContext(**values)
    if stop_after is not None:
        ctx.stop_requested = StopAfter(stop_after)
    return ctx


@dataclass
class StopAfter:
    """«Стоп» наступает после `polls` опросов `ctx.stop_requested()`."""

    polls: int
    seen: int = 0

    def __call__(self) -> bool:
        self.seen += 1
        return self.seen > self.polls
