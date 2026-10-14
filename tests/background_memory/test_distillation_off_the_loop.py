"""Where a distillation run's work happens, and the bounds that did not change with it."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest

from background_memory.env import MemoryEnv
from background_memory.handlers import distillation as distillation_mod
from background_memory.handlers.distillation import DistillationHandler
from background_memory.interfaces import MemoryDistillationEvent, MemoryTaskRef
from new_core.coordination.inprocess import InProcessBudget
from new_core.coordination.registry import MODEL_CALLS, TOOL_BODIES, concurrency_budgets
from ouroboros.observability import aef_tracing
from tests.helpers.background_memory_db import llm_double, memory_database
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.user_settings import stub_user_settings

_TRACE = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


@pytest.fixture
def db():
    yield from memory_database()


def _event(**kwargs) -> MemoryDistillationEvent:
    return MemoryDistillationEvent(
        scope=TEST_SCOPE,
        task=MemoryTaskRef(task_id="src-1", task_type="task", text="task"),
        response_text="A response long enough to be worth distilling. " * 4,
        **kwargs,
    )


def _handler(db, tmp_path, llm) -> DistillationHandler:
    return DistillationHandler(
        MemoryEnv(repo_dir=tmp_path, drive_root=tmp_path),
        llm,
        tasks=db.tasks,
        audit=db.audit,
        memory_headers=db.memory_headers,
        user_settings=stub_user_settings(main_model="primary-model", fallback_model=""),
    )


def _save_call(index: int, name: str) -> dict:
    return {
        "id": f"call-{index}",
        "function": {
            "name": "memory_save",
            "arguments": '{"name": "' + name + '", "content": "a durable fact", "memory_type": "project"}',
        },
    }


def _saving(name: str):
    answers = iter(
        [
            ({"content": "", "tool_calls": [_save_call(1, name)], "finish_reason": "tool_calls"}, {}),
            ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
        ]
    )
    return llm_double(lambda **_kwargs: next(answers))


def _eventually(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


class _AskedBudget(InProcessBudget):
    def __init__(self, limits: dict[str, int]) -> None:
        super().__init__(limits)
        self.asked = 0

    async def acquire_async(self, budget, **kwargs):
        if budget == TOOL_BODIES:
            self.asked += 1
        return await super().acquire_async(budget, **kwargs)


# --- the placement ------------------------------------------------------------


def test_a_hung_tool_body_does_not_prevent_a_later_tool_call(db, tmp_path, monkeypatch):
    """Inline bodies made the first hang the whole round; a shared pool would make it the process."""
    release = threading.Event()
    entered: list[str] = []

    def _body(_ctx, **kwargs):
        entered.append(str(kwargs.get("name") or ""))
        if kwargs.get("name") == "first":
            release.wait(timeout=30)
            return "✅ Memory 'first' saved"
        return "✅ Memory 'second' saved"

    monkeypatch.setattr(distillation_mod, "_DISTILL_TIMEOUT_SEC", 2.0)
    monkeypatch.setattr(
        distillation_mod,
        "_mem_tools",
        lambda ctx: [_entry("memory_save", _body), _entry("memory_list", _body)],
    )
    answers = iter(
        [
            (
                {
                    "content": "",
                    "tool_calls": [_save_call(1, "first"), _save_call(2, "second")],
                    "finish_reason": "tool_calls",
                },
                {},
            ),
            ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
        ]
    )

    result = db.run(_handler(db, tmp_path, llm_double(lambda **_kwargs: next(answers))).distil(_event()))

    release.set()
    assert result.status == "completed"
    assert entered == ["first", "second"], "the second body never ran"
    assert result.saved_names == ["second"], "the abandoned body's result was reported anyway"


def test_a_tool_body_runs_off_the_loop_and_sees_the_deliverys_operation(db, tmp_path, monkeypatch):
    """A body run with an empty context reports its outbound calls under no operation at all."""
    observed: dict[str, object] = {}

    def _body(_ctx, **_kwargs):
        observed["thread"] = threading.current_thread().name
        observed["trace_id"] = aef_tracing.current_trace_id()
        observed["headers"] = aef_tracing.with_trace_header({})
        return "✅ Memory 'seen' saved"

    monkeypatch.setattr(
        distillation_mod, "_mem_tools", lambda ctx: [_entry("memory_save", _body), _entry("memory_list", _body)]
    )
    answers = iter(
        [
            ({"content": "", "tool_calls": [_save_call(1, "seen")], "finish_reason": "tool_calls"}, {}),
            ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
        ]
    )

    db.run(_handler(db, tmp_path, llm_double(lambda **_kwargs: next(answers))).distil(_event(trace_id=_TRACE)))

    assert observed["thread"] != threading.main_thread().name
    assert observed["trace_id"] == _TRACE
    assert observed["headers"][aef_tracing.TRACE_HEADER_NAME] == _TRACE


def test_the_loop_keeps_running_while_a_tool_body_does(db, tmp_path, monkeypatch):
    """The whole point of the placement: another delivery's turn comes while a body works."""
    in_body = threading.Event()
    release = threading.Event()

    def _body(_ctx, **_kwargs):
        in_body.set()
        release.wait(timeout=10)
        return "✅ Memory 'slow' saved"

    monkeypatch.setattr(
        distillation_mod, "_mem_tools", lambda ctx: [_entry("memory_save", _body), _entry("memory_list", _body)]
    )
    answers = iter(
        [
            ({"content": "", "tool_calls": [_save_call(1, "slow")], "finish_reason": "tool_calls"}, {}),
            ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
        ]
    )
    handler = _handler(db, tmp_path, llm_double(lambda **_kwargs: next(answers)))
    neighbour: list[str] = []

    async def _scenario() -> None:
        running = asyncio.ensure_future(handler.distil(_event()))
        await asyncio.get_running_loop().run_in_executor(None, in_body.wait, 10.0)
        neighbour.append("ran")
        release.set()
        await running

    asyncio.run_coroutine_threadsafe(_scenario(), db.loop).result(timeout=30)

    assert neighbour == ["ran"]


def test_the_shared_process_wide_pool_is_not_used_for_a_tool_body() -> None:
    """`asyncio.to_thread` is the prohibited form (`new_core/tools/runtimes.py:24-46`)."""
    source = inspect.getsource(distillation_mod)

    assert "asyncio.to_thread(" not in source
    assert "ThreadPoolExecutor(max_workers=1" in source


def test_the_memory_tool_implementations_are_unchanged(tmp_path) -> None:
    """Owner decision 2: what changes is where a body runs, not what it does."""
    from ouroboros.tools import memory_tools

    entries = {entry.name: entry for entry in memory_tools.get_tools(_ctx(tmp_path))}

    assert {"memory_save", "memory_list"} <= set(entries)
    for name in ("memory_save", "memory_list"):
        assert not inspect.iscoroutinefunction(entries[name].handler)
        assert "ThreadPoolExecutor" not in inspect.getsource(entries[name].handler)
    assert "background_memory" not in inspect.getsource(memory_tools)


# --- the process's tool-body budget ----------------------------------------------


@pytest.mark.usefixtures("restore_process_singletons")
def test_two_concurrent_runs_hold_their_tool_bodies_to_a_process_limit_of_one(db, tmp_path, monkeypatch):
    budgets = _AskedBudget({MODEL_CALLS: 4, TOOL_BODIES: 1})
    concurrency_budgets.initialize(budgets)
    release = threading.Event()
    guard = threading.Lock()
    running: list[str] = []
    peak = 0
    in_use: list[int] = []

    def _body(_ctx, **kwargs):
        nonlocal peak
        name = str(kwargs.get("name") or "")
        with guard:
            running.append(name)
            peak = max(peak, len(running))
        in_use.append(budgets.in_use(TOOL_BODIES))
        release.wait(timeout=10)
        with guard:
            running.remove(name)
        return f"✅ Memory '{name}' saved"

    monkeypatch.setattr(
        distillation_mod, "_mem_tools", lambda ctx: [_entry("memory_save", _body), _entry("memory_list", _body)]
    )

    async def _both():
        return await asyncio.gather(
            *(_handler(db, tmp_path, _saving(name)).distil(_event()) for name in ("first", "second"))
        )

    both = asyncio.run_coroutine_threadsafe(_both(), db.loop)
    assert _eventually(lambda: budgets.asked == 2 or len(running) == 2), "the runs never reached their tool calls"
    release.set()
    results = both.result(timeout=30)

    assert (peak, in_use) == (1, [1, 1])
    assert [result.saved_names for result in results] == [["first"], ["second"]]
    assert budgets.in_use(TOOL_BODIES) == 0


@pytest.mark.usefixtures("restore_process_singletons")
def test_an_abandoned_tool_body_keeps_its_grant_until_it_finishes(db, tmp_path, monkeypatch):
    budgets = InProcessBudget({MODEL_CALLS: 4, TOOL_BODIES: 4})
    concurrency_budgets.initialize(budgets)
    release = threading.Event()

    def _body(_ctx, **_kwargs):
        release.wait(timeout=10)
        return "✅ Memory 'late' saved"

    monkeypatch.setattr(distillation_mod, "_DISTILL_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(distillation_mod, "_TOOL_BODY_FLOOR_SEC", 0.2)
    monkeypatch.setattr(
        distillation_mod, "_mem_tools", lambda ctx: [_entry("memory_save", _body), _entry("memory_list", _body)]
    )

    result = db.run(_handler(db, tmp_path, _saving("late")).distil(_event()))
    held_after_the_run = budgets.in_use(TOOL_BODIES)
    release.set()

    assert (result.status, result.saved_names, held_after_the_run) == ("completed", [], 1)
    assert _eventually(lambda: budgets.in_use(TOOL_BODIES) == 0), "the finished body kept its grant"


@pytest.mark.usefixtures("restore_process_singletons")
def test_a_tool_call_with_no_free_slot_by_its_deadline_runs_no_body(db, tmp_path, monkeypatch):
    budgets = InProcessBudget({MODEL_CALLS: 4, TOOL_BODIES: 1})
    occupied = budgets.acquire(TOOL_BODIES, holder="occupied", ttl_sec=60)
    concurrency_budgets.initialize(budgets)
    entered: list[str] = []

    def _body(_ctx, **_kwargs):
        entered.append("ran")
        return "✅ Memory 'blocked' saved"

    monkeypatch.setattr(distillation_mod, "_DISTILL_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(distillation_mod, "_TOOL_BODY_FLOOR_SEC", 0.2)
    monkeypatch.setattr(
        distillation_mod, "_mem_tools", lambda ctx: [_entry("memory_save", _body), _entry("memory_list", _body)]
    )

    try:
        result = db.run(_handler(db, tmp_path, _saving("blocked")).distil(_event()))
    finally:
        budgets.release(occupied)

    assert (result.status, result.saved_names, entered) == ("completed", [], [])


# --- the bounds that did not change --------------------------------------------


def test_the_preserved_distillation_bounds_are_the_numbers_they_were() -> None:
    assert distillation_mod._DISTILL_MAX_ROUNDS == 4
    assert distillation_mod._DISTILL_TIMEOUT_SEC == 45
    assert distillation_mod._DISTILL_MANIFEST_ENTRIES == 60
    assert distillation_mod._DISTILL_MANIFEST_CHARS == 4_000
    assert (
        distillation_mod._TASK_TEXT_CHARS,
        distillation_mod._RESPONSE_TEXT_CHARS,
        distillation_mod._TRACE_SUMMARY_CHARS,
    ) == (800, 1200, 1200)


def test_the_deadline_still_only_stops_a_new_round(db, tmp_path, monkeypatch):
    """An asynchronous rewrite invites cancelling the round in progress. It does not."""
    rounds: list[float] = []

    def chat(**_kwargs):
        rounds.append(time.monotonic())
        time.sleep(1.2)
        return {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}

    monkeypatch.setattr(distillation_mod, "_DISTILL_TIMEOUT_SEC", 1.0)

    result = db.run(_handler(db, tmp_path, llm_double(chat)).distil(_event()))

    assert len(rounds) == 1
    assert (result.status, result.rounds) == ("completed", 1), "the round in progress was cut short"


def _ctx(tmp_path):
    from tests.helpers.shared import make_tool_ctx

    return make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)


def _entry(name: str, handler):
    from types import SimpleNamespace

    return SimpleNamespace(name=name, handler=handler, schema={"type": "function", "function": {"name": name}})
