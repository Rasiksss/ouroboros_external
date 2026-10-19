"""Общие условия тестов инструментов цикла (шаг 13, CONTRACTS.md §3.0–§3.1).

Каталоги тестов не пакеты: опоры соседних каталогов (`tests/assistant`, `tests/dynamic_run`) подключаются
по имени, как это делают их собственные файлы. Рантайм с Case (`base`) и комната станции (`local_room`) —
те же, что у тестов `DynamicRun`; прогон, привязка и HTTP-клиент вместо плагина — те же, что у тестов `aifa_goal`.
"""
# ruff: noqa: E402, F401, F811 — фикстуры подключены общим conftest
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]
for sibling in ("assistant", "dynamic_run"):
    path = str(_TESTS / sibling)
    if path not in sys.path:
        sys.path.insert(0, path)

# Два каталога называют свой файл `conftest.py`: фикстуры комнаты берём по пути, под своим именем.
_spec = importlib.util.spec_from_file_location("dynamic_run_conftest", _TESTS / "dynamic_run" / "conftest.py")
assert _spec is not None and _spec.loader is not None
_dynamic_run = importlib.util.module_from_spec(_spec)
sys.modules["dynamic_run_conftest"] = _dynamic_run
_spec.loader.exec_module(_dynamic_run)
local_room = _dynamic_run.local_room
counting_room = _dynamic_run.counting_room

from test_ephemeral_in_graph import base, read_value
from tool_harness import coordination


import pytest

from finengine.assistant import goal_tools, read_tools
from finengine.config import settings


@pytest.fixture(autouse=True)
def loop_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base, coordination) -> None:
    """Срок вызова — доли секунды; состояние цикла — во временном каталоге; Case артефактов — рантайма теста."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "ouroboros_goal_wait_sec", 5.0)
    monkeypatch.setattr(settings, "ouroboros_run_watch_sec", 0.05)
    monkeypatch.setattr(goal_tools, "WAIT_MIN_SEC", 0)
    monkeypatch.setattr(read_tools, "get_case_store", lambda: base.store)
