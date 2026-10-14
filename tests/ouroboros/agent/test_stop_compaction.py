from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from ouroboros.agent.context_compaction import compact_tool_history_llm
from ouroboros.agent.loop_adapters import TranscriptCompactorAdapter
from ouroboros.agent.task_ledger import TaskLedger
from ouroboros.agent_runtime.cancellation import EventCancellation
from tests.helpers.messages import make_large_arg_messages
from tests.helpers.parametrize import parametrize_from_dict


def test_stopped_compaction_returns_before_the_provider_and_keeps_the_transcript(monkeypatch) -> None:
    """A late summary cannot mutate a stopped run or dispatch another batch."""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    cancellation = EventCancellation()
    calls = []

    def chat(_self, **kwargs):
        calls.append(kwargs)
        entered.set()
        try:
            assert release.wait(5)
            ids = re.findall(r"\[round:(\d+)\]", kwargs["messages"][-1]["content"])
            return {"content": "\n".join(f"[round:{i}]\nsummary" for i in ids)}, {}
        finally:
            finished.set()

    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", chat)
    messages = make_large_arg_messages("calculator", num_rounds=18)
    original = deepcopy(messages)
    adapter = TranscriptCompactorAdapter(
        drive_root=None,
        task_id="stopped",
        task_ledger=TaskLedger(),
        light_model="light-model",
        cancellation=cancellation,
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(adapter.compact, messages, keep_recent=1)
        assert entered.wait(2)
        cancellation.request_stop()
        assert future.result(timeout=1) is None
        assert not finished.is_set()
        assert messages == original
    finally:
        release.set()
        pool.shutdown(wait=True)
        assert finished.wait(2)
    assert len(calls) == 1
    assert calls[0]["stop_requested"]()
    assert messages == original


@parametrize_from_dict(
    {
        "before_first_batch": {"stop_before": True, "expected_calls": 0},
        "between_batches": {"stop_before": False, "expected_calls": 1},
    }
)
def test_compaction_starts_no_batch_after_stop(monkeypatch, stop_before, expected_calls) -> None:
    """Cancellation also wins when a summary returns without blocking."""
    cancellation = EventCancellation()
    calls = []

    def chat(_self, **kwargs):
        calls.append(kwargs)
        cancellation.request_stop()
        ids = re.findall(r"\[round:(\d+)\]", kwargs["messages"][-1]["content"])
        return {"content": "\n".join(f"[round:{i}]\nsummary" for i in ids)}, {}

    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", chat)
    messages = make_large_arg_messages("calculator", num_rounds=18)
    if stop_before:
        cancellation.request_stop()
    result = compact_tool_history_llm(
        messages, "light-model", keep_recent=1, cancellation=cancellation, task_id="stopped"
    )
    assert len(calls) == expected_calls
    assert result.messages == messages
    assert result.failure is None
    assert result.usage is None
