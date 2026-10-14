"""The slow-progress notice: the agent's sign of life while the user waits.

The notice rides the per-task heartbeat loop rather than a timer of its own.
Most tests call ``_slow_status_line`` — the decision — directly; the ones that
matter for wiring drive the real loop, because a decision nobody calls is worth
nothing.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import ouroboros.agent.core as core
from ouroboros.agent_events.interfaces import OutboundText, TaskHeartbeat
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.shared import TEST_SCOPE, FakeCancellation


@pytest.fixture
def agent(tmp_path):
    """A bare agent with just the fields the notice touches."""
    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    obj._events = RecordingAgentEvents()
    obj._current_task_id = "t-1"
    obj._current_task_type = "task"
    obj._last_progress_ts = 0.0
    # The loop keeps this locally; the fixture stands in for that local.
    obj._spoken = False
    # The loop also captures its emitter at heartbeat start; this stands in for it.
    obj._emit = obj._progress_emitter(TEST_SCOPE, {}, "t-1")
    # The supervisor's verdict is its own test below; here the task is alive.
    obj._still_running = lambda _task_id: True
    return obj


def _settings(enabled=True, sec=30):
    return SimpleNamespace(
        OUROBOROS_SLOW_LLM_STATUS_ENABLED=enabled,
        OUROBOROS_SLOW_LLM_STATUS_SEC=sec,
    )


def _spoken(agent) -> list[str]:
    """Progress texts the agent published, stripped of the 💬 marker."""
    out = [str(event.text).removeprefix("💬 ") for event in _progress_lines(agent)]
    agent._events.published.clear()
    return out


def _progress_lines(agent) -> list[OutboundText]:
    return [event for event in agent._events.published if isinstance(event, OutboundText) and event.is_progress]


def _tick(agent, monkeypatch, *, now, settings=None, task_id="t-1"):
    """One heartbeat tick: ask for the line, then emit it as the loop does."""
    monkeypatch.setattr(core.time, "monotonic", lambda: now)
    monkeypatch.setattr(
        "ouroboros.storage.settings.store.load_settings",
        lambda: settings if settings is not None else _settings(),
    )
    text = agent._slow_status_line(task_id, agent._spoken)
    if text is not None:
        agent._spoken = True
        agent._emit(text)


def test_silence_shorter_than_the_threshold_says_nothing(agent, monkeypatch):
    _tick(agent, monkeypatch, now=29.0)
    assert _spoken(agent) == []


def test_silence_past_the_threshold_explains_the_wait(agent, monkeypatch):
    _tick(agent, monkeypatch, now=31.0)
    assert _spoken(agent) == [core._SLOW_STATUS_TEXT]


def test_the_notice_is_off_when_the_flag_is_off(agent, monkeypatch):
    _tick(agent, monkeypatch, now=600.0, settings=_settings(enabled=False))
    assert _spoken(agent) == []


def test_anything_else_the_user_heard_postpones_the_notice(agent, monkeypatch):
    """The clock is 'when did the user last hear anything', not 'our own line',
    so a tool's progress message keeps this one quiet."""
    _tick(agent, monkeypatch, now=31.0)
    assert _spoken(agent) == [core._SLOW_STATUS_TEXT]

    # A real progress line at t=100 (emitting stamps _last_progress_ts).
    monkeypatch.setattr(core.time, "monotonic", lambda: 100.0)
    agent._emit("Читаю документы")
    assert _spoken(agent) == ["Читаю документы"]

    # t=155: 124s since our own line, so the repeat floor is already satisfied
    # and only the tool's message at t=100 (55s ago) can be keeping this quiet.
    _tick(agent, monkeypatch, now=155.0)
    assert _spoken(agent) == []


def test_repeats_are_shorter_and_keep_coming(agent, monkeypatch):
    """One line at minute one is no comfort at minute twenty."""
    _tick(agent, monkeypatch, now=31.0)
    _tick(agent, monkeypatch, now=155.0)
    _tick(agent, monkeypatch, now=280.0)

    assert _spoken(agent) == [
        core._SLOW_STATUS_TEXT,
        core._SLOW_STATUS_REPEAT_TEXT,
        core._SLOW_STATUS_REPEAT_TEXT,
    ]


def test_a_repeat_waits_out_the_quiet_period(agent, monkeypatch):
    _tick(agent, monkeypatch, now=31.0)
    _tick(agent, monkeypatch, now=100.0)  # only 69s after the first line
    assert _spoken(agent) == [core._SLOW_STATUS_TEXT]


def test_a_long_threshold_is_not_undercut_by_the_repeat_gap(agent, monkeypatch):
    """A deployment that only wants word after five minutes is not then nagged
    every two."""
    slow = _settings(sec=300)
    _tick(agent, monkeypatch, now=301.0, settings=slow)
    _tick(agent, monkeypatch, now=460.0, settings=slow)  # +159s: past the floor, short of 300

    assert _spoken(agent) == [core._SLOW_STATUS_TEXT]


def test_a_misconfigured_threshold_falls_back_to_the_default(agent, monkeypatch):
    """'30s' and other typos coerce to 0 in the schema; 0 must not become the
    chattiest possible setting."""
    from ouroboros.storage.settings.schema import get_defaults

    broken = _settings(sec=0)
    _tick(agent, monkeypatch, now=float(get_defaults().OUROBOROS_SLOW_LLM_STATUS_SEC) - 1, settings=broken)
    assert _spoken(agent) == []

    _tick(agent, monkeypatch, now=float(get_defaults().OUROBOROS_SLOW_LLM_STATUS_SEC) + 1, settings=broken)
    assert _spoken(agent) == [core._SLOW_STATUS_TEXT]


def test_the_notice_does_not_outlive_its_own_task(agent, monkeypatch):
    """The heartbeat thread can reach this a tick after the task ended, and by
    then the agent's task fields belong to whatever runs next."""
    agent._current_task_id = "t-2"
    _tick(agent, monkeypatch, now=600.0, task_id="t-1")
    assert _spoken(agent) == []


def test_heartbeat_hands_back_a_thread_the_caller_can_join(tmp_path):
    """The race this closes: a tick already past its task check must finish
    before handle_task rebinds the task fields to the next task, or the notice
    lands in the wrong chat. Stopping without joining does not close it."""
    import threading

    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    obj._events = RecordingAgentEvents()
    obj._current_task_id = "t-1"
    obj._current_task_type = "task"
    obj._last_progress_ts = 0.0

    started = core.OuroborosAgent._start_task_heartbeat_loop(obj, "t-1", TEST_SCOPE, {})
    assert started is not None
    stop, thread = started
    assert isinstance(stop, threading.Event)
    assert isinstance(thread, threading.Thread)

    stop.set()
    thread.join(timeout=core._HEARTBEAT_JOIN_TIMEOUT_SEC)
    assert not thread.is_alive(), "setting the stop event must end the loop"


def test_handle_task_stops_and_joins_the_heartbeat_before_clearing_task_fields(tmp_path, monkeypatch):
    """Behavioural, not a source-order pin: a join(timeout=0) or a missing
    stop() both keep the lines in order yet leave the race wide open.

    The injected heartbeat records what it sees the moment it is released. If
    handle_task cleared the task fields first, or never waited, the thread
    would observe them already gone.
    """
    import threading
    import time

    from tests.helpers.agent import make_agent

    agent, _ = make_agent(tmp_path)
    agent._emit_live_log = lambda *a, **k: None
    agent._task_graph = SimpleNamespace(invoke=lambda *a, **kw: None)

    seen: dict = {}
    released = threading.Event()

    def _fake_heartbeat(task_id: str, scope, transport, *, detached=False, claim_token=""):
        assert detached is False
        stop = threading.Event()

        def _body() -> None:
            stop.wait(5)
            # Long enough that a handle_task which does not wait would have
            # cleared the fields before this line runs.
            time.sleep(0.2)
            seen["task_id"] = agent._current_task_id
            released.set()

        thread = threading.Thread(target=_body, daemon=True)
        thread.start()
        return stop, thread

    monkeypatch.setattr(agent, "_start_task_heartbeat_loop", _fake_heartbeat)

    agent.handle_task(
        {"id": "task-1", "type": "task", "text": "go", "scope": TEST_SCOPE.to_dict()},
        cancellation=FakeCancellation(),
    )

    assert released.is_set(), "handle_task must wait for the heartbeat, not just signal it"
    assert seen["task_id"] == "task-1", "the fields must still be this task's when the tick is released"


def test_the_notice_is_addressed_when_the_heartbeat_starts_not_when_it_speaks(tmp_path):
    """The race this closes: the thread can be descheduled past its own checks,
    and by then the live task fields belong to the NEXT task. The emitter is
    captured at heartbeat start, so a late line still carries its own task."""
    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    obj._events = RecordingAgentEvents()
    obj._current_task_id = "t-1"
    obj._last_progress_ts = 0.0

    emit = obj._progress_emitter(TEST_SCOPE, {"conv": "A"}, "t-1")

    # The next task takes the agent over while a leaked tick is still pending.
    obj._current_task_id = "t-2"

    emit(core._SLOW_STATUS_TEXT)

    published = _progress_lines(obj)
    assert published, "no progress event was emitted"
    line = published[0]
    assert line.task_id == "t-1", "a late notice must stay stamped with its own task"
    assert line.scope == TEST_SCOPE
    assert line.transport == {"conv": "A"}


def test_progress_carries_the_scope_and_task_it_was_built_for(tmp_path):
    """The loop's own progress must keep working exactly as before."""
    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    obj._events = RecordingAgentEvents()
    obj._last_progress_ts = 0.0

    obj._progress_emitter(TEST_SCOPE, {}, "t-9")("Читаю документы")

    (event,) = _progress_lines(obj)
    assert event.scope == TEST_SCOPE and event.task_id == "t-9"
    assert event.source == TEST_SCOPE.surface_id


def test_the_notice_goes_quiet_once_the_supervisor_has_written_the_task_off(agent, monkeypatch):
    """Nothing can interrupt a running task thread, so a killed, cancelled or
    restarted task keeps working. The supervisor has already told the user it is
    over; reassuring past that point contradicts it."""
    agent._still_running = lambda _task_id: False
    _tick(agent, monkeypatch, now=600.0)
    assert _spoken(agent) == []

    agent._still_running = lambda _task_id: True
    _tick(agent, monkeypatch, now=600.0)
    assert _spoken(agent) == [core._SLOW_STATUS_TEXT]


@pytest.mark.parametrize(
    ("status", "still_running"),
    [("running", True), ("cancelled", False), ("failed", False), ("completed", False)],
)
def test_still_running_reads_the_supervisors_verdict(tmp_path, monkeypatch, status, still_running):
    """One stored status covers every way a task ends without its thread
    stopping: hard timeout, cancellation, and owner restart."""
    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    monkeypatch.setattr(core, "load_task_result", lambda _root, _task_id: {"status": status})

    assert obj._still_running("t-1") is still_running


def test_still_running_assumes_alive_when_the_status_cannot_be_read(tmp_path, monkeypatch):
    """A store hiccup must not silence a task that is genuinely working."""
    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)

    def _boom(_root, _task_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(core, "load_task_result", _boom)

    assert obj._still_running("t-1") is True


def test_the_real_heartbeat_loop_addresses_the_task_it_started_with(tmp_path, monkeypatch):
    """The wiring test the direct-call ones cannot be: it drives the real loop
    and reproduces the race the capture exists for.

    Everything else here calls ``_slow_status_line`` and emits by hand, which
    proves the decision but keeps passing even if the loop stops calling it. Here
    the agent's live task fields are rebound *between* the decision and the emit,
    exactly as the next task would rebind them, so a line addressed from live
    fields would carry t-2 and one from the captured emitter carries t-1.
    """
    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    obj._events = RecordingAgentEvents()
    obj._current_task_id = "t-1"
    obj._current_task_type = "task"
    obj._last_progress_ts = 0.0
    obj._still_running = lambda _task_id: True

    monkeypatch.setattr(core.time, "monotonic", lambda: 10_000.0)
    monkeypatch.setattr("ouroboros.storage.settings.store.load_settings", lambda: _settings())
    monkeypatch.setattr(core, "HEARTBEAT_PERIOD_SEC", 0.01)

    decided = core.OuroborosAgent._slow_status_line

    def _decide_then_hand_the_agent_to_the_next_task(self, task_id, spoken):
        text = decided(self, task_id, spoken)
        if text is not None:
            self._current_task_id = "t-2"
        return text

    monkeypatch.setattr(core.OuroborosAgent, "_slow_status_line", _decide_then_hand_the_agent_to_the_next_task)

    heartbeat = obj._start_task_heartbeat_loop("t-1", TEST_SCOPE, {"conv": "A"})
    assert heartbeat is not None
    stop, thread = heartbeat
    try:
        deadline = time.time() + 5
        while not _peek_progress(obj) and time.time() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)

    lines = _progress_lines(obj)
    assert lines, "the real loop never emitted the notice"
    assert str(lines[0].text).removeprefix("💬 ") == core._SLOW_STATUS_TEXT
    assert lines[0].task_id == "t-1", "the line must carry the task captured at heartbeat start"
    assert lines[0].scope == TEST_SCOPE
    assert lines[0].transport == {"conv": "A"}


def test_a_detached_heartbeat_renews_the_lease_without_owner_progress(tmp_path, monkeypatch):
    obj = object.__new__(core.OuroborosAgent)
    obj.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    obj._events = RecordingAgentEvents()
    obj._current_task_id = "t-1"
    obj._current_task_type = "task"
    obj._last_progress_ts = 0.0
    obj._still_running = lambda _task_id: True
    monkeypatch.setattr(core, "HEARTBEAT_PERIOD_SEC", 0.01)
    monkeypatch.setattr(core.OuroborosAgent, "_slow_status_line", lambda *_args: "still running")

    started = obj._start_task_heartbeat_loop("t-1", TEST_SCOPE, {}, detached=True)
    assert started is not None
    stop, thread = started
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(isinstance(event, TaskHeartbeat) and event.phase == "running" for event in obj._events.published):
                break
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not _progress_lines(obj)
    assert any(isinstance(event, TaskHeartbeat) and event.phase == "running" for event in obj._events.published)


def _peek_progress(agent) -> bool:
    return bool(_progress_lines(agent))
