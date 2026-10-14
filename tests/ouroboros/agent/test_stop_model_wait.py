from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from new_core.coordination.inprocess import InProcessBudget
from new_core.coordination.registry import MODEL_CALLS, concurrency_budgets
from new_core.llm.types import LLMFailure
from ouroboros.agent.loop_llm_call import call_llm_with_retry
from ouroboros.agent_events.interfaces import LlmUsageReported
from ouroboros.agent_runtime.cancellation import EventCancellation, StoreCancellation
from ouroboros.llm import LLMClient
from ouroboros.llm.circuit_breaker import (
    provider_circuit_is_open,
    provider_circuit_last_error,
    record_provider_failure,
    reset_provider_circuit_breakers,
)
from ouroboros.llm.providers import OpenAICompatibleProvider
from ouroboros.llm.registry import ProviderRegistry
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import execution_claim
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.factories import make_openai_compat_fake
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


@parametrize_from_dict(
    {
        "stop_before_acquire": {"loss": "stop", "after_grant": False},
        "stop_after_grant": {"loss": "stop", "after_grant": True},
        "claim_before_acquire": {"loss": "claim", "after_grant": False},
        "claim_after_grant": {"loss": "claim", "after_grant": True},
    }
)
def test_execution_claim_loss_does_not_retry_or_trip_the_provider_breaker(tmp_path, monkeypatch, loss, after_grant):
    """Durable cancellation wins even while the stop signal still holds a cached active read."""
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "test-key")
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="task-claim", detached=True, scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="test")
    assert claimed is not None
    claim_token = claimed.metadata["execution_claim"]
    cancellation = StoreCancellation(
        store.tasks_repo,
        task_id=claimed.task_id,
        scope=TEST_SCOPE,
        claim_token=claim_token,
        poll_interval_sec=3600,
    )
    assert not cancellation.is_stopped()

    def revoke_claim():
        if loss == "stop":
            store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id=claimed.job_id)
        else:
            claimed.metadata["execution_claim"] = "replacement"
            store.tasks_repo.upsert_task(claimed)

    class RevokingBudget(InProcessBudget):
        def acquire(self, *args, **kwargs):
            lease = super().acquire(*args, **kwargs)
            if after_grant:
                revoke_claim()
            return lease

    class CountingClient(LLMClient):
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            return super().chat(**kwargs)

    seen = {}
    provider = OpenAICompatibleProvider("cloudru", client_factory=lambda **_kwargs: make_openai_compat_fake(seen=seen))
    client = CountingClient(registry=ProviderRegistry([provider]))
    model = "cloudru::test-model"
    budgets = RevokingBudget({MODEL_CALLS: 1})
    concurrency_budgets.initialize(budgets)
    reset_provider_circuit_breakers()
    token = execution_claim.set((claimed.task_id, claim_token, cancellation.is_stopped))
    try:
        record_provider_failure(model, error=RuntimeError("previous provider failure"))
        assert provider_circuit_last_error(model) == "previous provider failure"
        if not after_grant:
            revoke_claim()
        result = call_llm_with_retry(
            client,
            [],
            model,
            None,
            "none",
            3,
            tmp_path / "logs",
            claimed.task_id,
            1,
            None,
            {},
            scope=TEST_SCOPE,
            cancellation=cancellation,
            audit_event_repository=sql_audit_event_repository(),
        )
        assert result == LLMFailure("Task stopped")
        assert client.calls == 1
        assert seen == {}
        assert budgets.in_use(MODEL_CALLS) == 0
        assert not provider_circuit_is_open(model)
        assert provider_circuit_last_error(model) == "previous provider failure"
    finally:
        execution_claim.reset(token)
        concurrency_budgets.reset_for_tests()
        reset_provider_circuit_breakers()


@parametrize_from_dict({"late": {"stop": True}, "normal": {"stop": False}})
def test_model_usage_is_reported_once_even_after_stop(tmp_path, stop) -> None:
    """An abandoned response retains attributed usage without changing the closed run's counters."""
    entered, release, reported = threading.Event(), threading.Event(), threading.Event()
    cancellation = EventCancellation()
    accumulated = {}

    class Publisher(RecordingAgentEvents):
        def publish(self, event):
            super().publish(event)
            if isinstance(event, LlmUsageReported):
                reported.set()

    class Client:
        def chat(self, **_kwargs):
            entered.set()
            assert release.wait(5)
            return {"content": "answer"}, {
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "resolved_model": "actual-model",
                "provider": "cloudru",
            }

    events = Publisher()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            call_llm_with_retry,
            Client(),
            [],
            "model",
            None,
            "none",
            1,
            tmp_path / "logs",
            "task-usage",
            1,
            events,
            accumulated,
            scope=TEST_SCOPE,
            cancellation=cancellation,
            audit_event_repository=sql_audit_event_repository(),
        )
        assert entered.wait(2)
        if stop:
            cancellation.request_stop()
            assert isinstance(future.result(timeout=2), LLMFailure)
            assert not reported.is_set()
        release.set()
        future.result(timeout=2)
        assert reported.wait(2)
        usage_events = events.of_type(LlmUsageReported)
        assert len(usage_events) == 1
        event = usage_events[0]
        assert (event.scope, event.task_id, event.model, event.provider) == (
            TEST_SCOPE,
            "task-usage",
            "actual-model",
            "cloudru",
        )
        assert (event.prompt_tokens, event.completion_tokens) == (12, 7)
        if stop:
            assert accumulated == {}
        else:
            assert accumulated["prompt_tokens"] == 12
    finally:
        release.set()
        pool.shutdown(wait=True)


class _BlockingLLM:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def chat(self, **_kwargs):
        self.entered.set()
        self.release.wait(5)
        self.finished.set()
        return {"content": "too late", "tool_calls": []}, {}


def test_a_stopped_run_does_not_wait_for_the_synchronous_model_result(tmp_path) -> None:
    llm = _BlockingLLM()
    cancellation = EventCancellation()
    returned = threading.Event()
    result: list[object] = []

    def call() -> None:
        result.append(
            call_llm_with_retry(
                llm,
                [],
                "model",
                None,
                "none",
                1,
                tmp_path / "logs",
                "task-1",
                1,
                None,
                {},
                scope=TEST_SCOPE,
                cancellation=cancellation,
                audit_event_repository=sql_audit_event_repository(),
            )
        )
        returned.set()

    thread = threading.Thread(target=call)
    thread.start()
    assert llm.entered.wait(2)
    try:
        cancellation.request_stop()
        assert returned.wait(1), "the task kept waiting for the provider response"
        assert isinstance(result[0], LLMFailure)
    finally:
        llm.release.set()
        thread.join(2)
        assert llm.finished.wait(2)


def test_foreground_stop_ends_the_model_slot_wait_without_dispatch(tmp_path, monkeypatch) -> None:
    """Stopping a foreground run also ends its provider thread waiting for a model slot."""
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "test-key")
    monkeypatch.setenv("OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC", "0.05")
    entered, finished = threading.Event(), threading.Event()
    seen = {}

    class ObservedBudget(InProcessBudget):
        def acquire(self, budget, *, holder, **kwargs):
            if holder != "occupied":
                entered.set()
            return super().acquire(budget, holder=holder, **kwargs)

    class ObservedClient(LLMClient):
        def chat(self, **kwargs):
            try:
                return super().chat(**kwargs)
            finally:
                finished.set()

    budgets = ObservedBudget({MODEL_CALLS: 1})
    held = budgets.acquire(MODEL_CALLS, holder="occupied", ttl_sec=60)
    concurrency_budgets.initialize(budgets)
    provider = OpenAICompatibleProvider("cloudru", client_factory=lambda **_kwargs: make_openai_compat_fake(seen=seen))
    llm = ObservedClient(registry=ProviderRegistry([provider]))
    cancellation = EventCancellation()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            call_llm_with_retry,
            llm,
            [],
            "cloudru::test-model",
            None,
            "none",
            1,
            tmp_path / "logs",
            "foreground-task",
            1,
            None,
            {},
            scope=TEST_SCOPE,
            cancellation=cancellation,
            audit_event_repository=sql_audit_event_repository(),
        )
        assert entered.wait(2)
        cancellation.request_stop()
        assert isinstance(future.result(timeout=2), LLMFailure)
        assert finished.wait(2), "the cancelled call still waits for a model slot"
        assert budgets.in_use(MODEL_CALLS) == 1
        assert seen == {}
    finally:
        budgets.release(held)
        pool.shutdown(wait=True)
        finished.wait(2)
        concurrency_budgets.reset_for_tests()
