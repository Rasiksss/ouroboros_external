import json
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from new_core.llm.types import LLMFailure
from new_core.loop._helpers import provider_failure_hint
from ouroboros.agent.loop_llm_call import call_llm_with_retry
from ouroboros.agent_events.interfaces import LiveLogLine
from ouroboros.llm.circuit_breaker import (
    CircuitBreaker,
    LLMFailureCategory,
    ProviderTarget,
    classify_failure,
    provider_failure_category,
    reset_circuit_breakers,
)
from ouroboros.llm.client import InvalidResponse, LocalContextTooLargeError
from ouroboros.llm.providers.base import ProviderRequestError, ProviderResponseError
from ouroboros.llm.providers.gigachat_b2bank import B2BankModelUnavailableError
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE, FakeCancellation


def _model_unavailable_error():
    request = httpx.Request("POST", "https://b2bank.test/v1/chat/completions")
    return B2BankModelUnavailableError(
        "B2Bank chat HTTP status 403; request_id=r1; reason=model_temporarily_unavailable",
        request=request,
        response=httpx.Response(403, request=request),
    )


@pytest.fixture
def breaker_log(monkeypatch):
    """Standard chat_with_fallback environment: closed circuits, recorded breaker mutations."""
    from ouroboros.llm import client as llm_client

    log = SimpleNamespace(failures=[], successes=[])
    monkeypatch.setattr(llm_client, "provider_base_url_for_model", lambda model: f"https://{model}")
    monkeypatch.setattr(llm_client, "provider_circuit_is_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        llm_client,
        "record_provider_failure",
        lambda model, **kwargs: (
            log.failures.append((model, kwargs.get("category"), str(kwargs.get("error") or ""))) or False
        ),
    )
    monkeypatch.setattr(llm_client, "record_provider_success", lambda model, **_kwargs: log.successes.append(model))
    return log


def _chat_with_fallback(chat, **overrides):
    from ouroboros.llm import client as llm_client

    kwargs = {"model": "primary-model", "fallback_model": "fallback-model", "deadline": None, "timeout_sec": 30}
    kwargs.update(overrides)
    return llm_client.chat_with_fallback(type("LLM", (), {"chat": staticmethod(chat)})(), **kwargs)


def _http_error(status, message="opaque provider error", body=""):
    request = httpx.Request("POST", "https://llm.invalid")
    return httpx.HTTPStatusError(message, request=request, response=httpx.Response(status, request=request, text=body))


class _SuccessfulLLM:
    def chat(self, **kwargs):
        return {"content": "ok"}, {"provider": "anthropic", "resolved_model": "anthropic/claude-sonnet-4-6"}


@pytest.mark.parametrize(
    ("error", "category"),
    [
        pytest.param(RuntimeError("401 invalid_api_key"), LLMFailureCategory.AUTH, id="auth"),
        pytest.param(
            RuntimeError("provider diagnostics " * 20 + "context length exceeded"),
            LLMFailureCategory.CONTEXT,
            id="overflow-after-220-characters",
        ),
        pytest.param(
            RuntimeError("request contains 250000 tokens, maximum allowed is 200000"),
            LLMFailureCategory.CONTEXT,
            id="maximum-allowed-tokens",
        ),
        pytest.param(LocalContextTooLargeError("opaque local error"), LLMFailureCategory.CONTEXT, id="local"),
        pytest.param(
            _http_error(400, body="provider diagnostics " * 20 + "context length exceeded"),
            LLMFailureCategory.CONTEXT,
            id="http-400-context",
        ),
        pytest.param(_http_error(400, "invalid tool schema"), LLMFailureCategory.BAD_REQUEST, id="http-400"),
        pytest.param(
            _http_error(429, "token limit exceeded", "context length exceeded"),
            LLMFailureCategory.TRANSIENT,
            id="http-429",
        ),
        pytest.param(RuntimeError("HTTP 429: token limit reached"), LLMFailureCategory.TRANSIENT, id="text-429"),
        pytest.param(
            RuntimeError("Rate limit reached on tokens per min (TPM): Limit 10000, Used 10000."),
            LLMFailureCategory.TRANSIENT,
            id="token-rate-limit",
        ),
        pytest.param(TimeoutError("temporary network failure"), LLMFailureCategory.TRANSIENT, id="timeout"),
    ],
)
def test_call_llm_with_retry_records_full_error_category_before_truncating(tmp_path, monkeypatch, error, category):
    from ouroboros.agent import loop_llm_call

    llm = Mock(spec=["chat"])
    llm.chat.side_effect = error
    sleep = Mock()
    failures = Mock()
    monkeypatch.setattr(loop_llm_call.time, "sleep", sleep)
    monkeypatch.setattr(loop_llm_call, "provider_circuit_is_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(loop_llm_call, "record_provider_failure", failures)
    usage = {}

    msg = call_llm_with_retry(
        llm,
        [{"role": "user", "content": "hi"}],
        "openai::gpt-5.5",
        None,
        "medium",
        3,
        tmp_path,
        "task-1",
        1,
        None,
        usage,
        "task",
        False,
        scope=TEST_SCOPE,
        cancellation=FakeCancellation(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert isinstance(msg, LLMFailure)
    overflow = category is LLMFailureCategory.CONTEXT
    assert msg.context_overflow is overflow
    assert not msg.model_unavailable
    assert llm.chat.call_count == (1 if overflow else 3)
    assert sum(entry.args[0] for entry in sleep.call_args_list) == pytest.approx(0 if overflow else 6)
    assert [entry.kwargs["category"] for entry in failures.call_args_list] == [category] * llm.chat.call_count
    assert all(entry.kwargs["error"] is error for entry in failures.call_args_list)
    assert len(msg.detail) <= 220
    if len(repr(error)) > 220:
        assert msg.detail.endswith("...") and "context length exceeded" not in msg.detail
    assert usage == {}


def test_provider_failure_hint_formats_detail():
    hint = provider_failure_hint(LLMFailure("  AuthenticationError('401 invalid_api_key')  "))

    assert hint == " Last provider error: AuthenticationError('401 invalid_api_key')"


def test_provider_failure_hint_empty_without_error():
    assert provider_failure_hint(None) == ""


def test_call_llm_with_retry_stops_immediately_on_model_unavailable(tmp_path):
    calls = []

    class _UnavailableLLM:
        def chat(self, **kwargs):
            calls.append(kwargs)
            raise _model_unavailable_error()

    usage = {}
    live = RecordingAgentEvents()
    msg = call_llm_with_retry(
        _UnavailableLLM(),
        [{"role": "user", "content": "hi"}],
        "gigachat::GigaChat-2-Max",
        None,
        "medium",
        3,
        tmp_path,
        "task-stop",
        1,
        live,
        usage,
        "task",
        False,
        scope=TEST_SCOPE,
        cancellation=FakeCancellation(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert isinstance(msg, LLMFailure)
    assert len(calls) == 1
    assert msg.model_unavailable
    assert "model_temporarily_unavailable" in msg.detail
    # The round announced itself as started, so it has to announce how it ended.
    kinds = [event.data["type"] for event in live.of_type(LiveLogLine)]
    assert kinds == ["llm_round_started", "llm_round_error"]
    # Nothing from the failed call is parked in the task's usage dict.
    assert usage == {}


def test_call_llm_with_retry_accumulates_usage_tokens(tmp_path):
    class _TokenReportingLLM:
        def chat(self, **kwargs):
            return (
                {"content": "ok"},
                {
                    "provider": "openai",
                    "resolved_model": "openai/gpt-5.5",
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )

    usage = {}
    call_llm_with_retry(
        _TokenReportingLLM(),
        [{"role": "user", "content": "hi"}],
        "openai::gpt-5.5",
        None,
        "medium",
        1,
        tmp_path,
        "task-3",
        1,
        None,
        usage,
        "task",
        False,
        scope=TEST_SCOPE,
        cancellation=FakeCancellation(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert usage["prompt_tokens"] == 1000
    assert usage["completion_tokens"] == 100


def test_circuit_breaker_opens_only_for_transient_failures():
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=60)
    target = ProviderTarget(
        provider="selfhosted",
        base_url="https://llm.local/v1?token=secret",
        resolved_model="llama",
        transport_mode="remote",
    )

    breaker.record_failure(target, LLMFailureCategory.AUTH)
    breaker.record_failure(target, LLMFailureCategory.BAD_REQUEST)
    breaker.record_failure(target, LLMFailureCategory.MODEL_UNAVAILABLE)
    assert breaker.state(target).state == "closed"

    breaker.record_failure(target, LLMFailureCategory.TRANSIENT)
    assert breaker.state(target).state == "closed"
    breaker.record_failure(target, LLMFailureCategory.TRANSIENT)

    state = breaker.state(target)
    assert state.state == "open"
    assert "token=" not in state.identity


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "auth"),
        (400, "bad_request"),
        (404, "config"),
        (429, "provider_outage"),
        (500, "provider_outage"),
        (502, "provider_outage"),
        (503, "provider_outage"),
        (504, "provider_outage"),
    ],
)
def test_http_status_failure_category_uses_typed_response(status, expected):
    request = httpx.Request("POST", "https://llm.invalid")
    error = httpx.HTTPStatusError(
        "opaque provider error",
        request=request,
        response=httpx.Response(status, request=request),
    )
    assert provider_failure_category(error) == expected


@pytest.mark.parametrize("in_response", [False, True])
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("context length exceeded", LLMFailureCategory.CONTEXT),
        ("request contains 250000 tokens, maximum allowed is 200000", LLMFailureCategory.CONTEXT),
        ("invalid tool schema", LLMFailureCategory.BAD_REQUEST),
    ],
)
def test_http_400_classifies_the_complete_error_or_response(in_response, message, expected):
    detail = "provider diagnostics " * 20 + message
    error = _http_error(400, "opaque provider error" if in_response else detail, detail if in_response else "")
    assert classify_failure(error) is expected


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit reached on tokens per min (TPM): Limit 10000, Used 10000.",
        "HTTP 429: token limit reached",
        "rate_limit_exceeded: tokens",
    ],
)
def test_token_rate_limits_are_transient(message):
    assert classify_failure(RuntimeError(message)) is LLMFailureCategory.TRANSIENT


@pytest.mark.parametrize(
    ("message", "cause", "expected"),
    [
        ("provider request failed", httpx.ReadError("response read failed"), "provider_outage"),
        ("provider timeout", TypeError("adapter bug"), "internal"),
    ],
)
def test_typed_cause_precedes_wrapper_text(message, cause, expected):
    error = RuntimeError(message)
    error.__cause__ = cause
    assert provider_failure_category(error) == expected


def test_model_unavailable_error_has_own_category():
    error = _model_unavailable_error()

    assert classify_failure(error) is LLMFailureCategory.MODEL_UNAVAILABLE
    assert provider_failure_category(error) == "model_unavailable"


def test_provider_response_error_is_incomplete():
    assert provider_failure_category(ProviderResponseError("invalid response")) == "incomplete_response"


def test_provider_request_error_is_unsupported_request():
    assert provider_failure_category(ProviderRequestError("unrepresentable request")) == "unsupported_request"


def test_cyclic_failure_cause_defaults_to_provider_outage():
    outer = RuntimeError("outer")
    inner = RuntimeError("inner")
    outer.__cause__ = inner
    inner.__cause__ = outer

    # Cycle guard terminates; an unrecognized error defaults to a provider
    # fault so the breaker can open and background routing may fall back.
    assert provider_failure_category(outer) == "provider_outage"


def test_unrecognized_provider_error_still_opens_breaker():
    """Regression: the foreground path records failures without a category and
    relies on classify_failure defaulting unknown provider errors to TRANSIENT."""
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=60)
    target = ProviderTarget("selfhosted", "https://llm.local/v1", "llama", "remote")
    category = classify_failure(RuntimeError("upstream connect error or disconnect/reset before headers"))

    breaker.record_failure(target, category)
    breaker.record_failure(target, category)

    assert breaker.state(target).state == "open"


def test_internal_json_decode_error_stays_internal():
    assert provider_failure_category(json.JSONDecodeError("invalid", "x", 0)) == "internal"


def test_circuit_breaker_success_closes_state():
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=60)
    target = ProviderTarget("gigachat-b2bank", "https://giga.local/v1", "GigaChat", "remote")

    breaker.record_failure(target, LLMFailureCategory.TRANSIENT)
    assert breaker.is_open(target)

    breaker.record_success(target)

    assert breaker.state(target).state == "closed"


def test_circuit_breaker_reset_matching_targets_on_user_input():
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=60)
    active = ProviderTarget("selfhosted", "https://llm.local/v1", "llama", "remote")
    other = ProviderTarget("cloudru", "https://foundation-models.cloud.ru/v1", "glm", "remote")

    breaker.record_failure(active, LLMFailureCategory.TRANSIENT)
    breaker.record_failure(other, LLMFailureCategory.TRANSIENT)
    assert breaker.is_open(active)
    assert breaker.is_open(other)

    breaker.reset_targets([active])

    assert not breaker.is_open(active)
    assert breaker.is_open(other)


def test_reset_circuit_breakers_clears_global_state():
    target = ProviderTarget("selfhosted", "https://llm.local/v1", "llama", "remote")
    breaker = CircuitBreaker.global_instance()
    breaker.record_failure(target, LLMFailureCategory.TRANSIENT)
    breaker.record_failure(target, LLMFailureCategory.TRANSIENT)
    assert breaker.state(target).state in {"open", "half_open"}

    reset_circuit_breakers()

    assert breaker.state(target).state == "closed"


def test_background_fallback_skips_open_primary_circuit(monkeypatch, breaker_log):
    from ouroboros.llm import client as llm_client

    calls = []
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(llm_client, "provider_circuit_is_open", lambda model, **_kwargs: model == "primary-model")

    def chat(**kwargs):
        calls.append(kwargs)
        return {"content": "ok"}, {}

    message, _usage, used_model = _chat_with_fallback(chat, deadline=130.0)

    assert (message["content"], used_model) == ("ok", "fallback-model")
    assert (calls[0]["model"], calls[0]["deadline"]) == ("fallback-model", 130.0)
    assert breaker_log.successes == ["fallback-model"]


def test_background_fallback_deduplicates_resolved_target_aliases(monkeypatch):
    # Deliberately keeps the real provider_base_url_for_model/provider_circuit_key:
    # the dedup under test happens inside that alias resolution.
    from ouroboros.llm import client as llm_client

    calls = []
    monkeypatch.setattr(llm_client, "provider_circuit_is_open", lambda *args, **kwargs: False)
    monkeypatch.setattr(llm_client, "record_provider_failure", lambda *args, **kwargs: False)

    def chat(**kwargs):
        calls.append(kwargs["model"])
        raise TimeoutError("provider timed out")

    with pytest.raises(RuntimeError, match="Background LLM attempts failed"):
        _chat_with_fallback(chat, model="cloudru::zai-org/GLM-4.7", fallback_model="cloudru/zai-org/GLM-4.7")

    assert calls == ["cloudru::zai-org/GLM-4.7"]


def test_background_failure_preserves_primary_and_fallback_causes(breaker_log):
    def chat(**kwargs):
        if kwargs["model"] == "primary-model":
            raise TimeoutError("primary timed out")
        raise RuntimeError("AuthenticationError: 401 Bearer eyJhbGciOiJIUzI1NiJ9.secret-token-value")

    with pytest.raises(RuntimeError) as caught:
        _chat_with_fallback(chat)

    error = str(caught.value)
    assert "primary-model [provider_outage/TimeoutError]" in error
    assert "fallback-model [auth/RuntimeError]" in error
    assert "secret-token-value" not in error
    assert caught.value.__cause__ is None
    assert all("secret-token-value" not in detail for _model, _category, detail in breaker_log.failures)


@pytest.mark.parametrize("error", [TypeError("adapter bug"), AttributeError("adapter bug")])
def test_background_internal_error_is_not_retried_or_recorded(breaker_log, error):
    calls = []

    def chat(**kwargs):
        calls.append(kwargs["model"])
        raise error

    with pytest.raises(type(error)):
        _chat_with_fallback(chat)

    assert calls == ["primary-model"]
    assert breaker_log.failures == []


def test_background_opaque_error_is_retried_on_fallback_and_recorded(breaker_log):
    """Regression: an unrecognized provider error must not be mistaken for our
    bug — it tries the fallback and counts toward the primary's breaker."""
    calls = []

    def chat(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "primary-model":
            raise RuntimeError("opaque provider failure")
        return {"content": "ok"}, {}

    message, _usage, used_model = _chat_with_fallback(chat)

    assert (message["content"], used_model) == ("ok", "fallback-model")
    assert calls == ["primary-model", "fallback-model"]
    assert [model for model, _category, _error in breaker_log.failures] == ["primary-model"]


def test_background_request_error_falls_back_without_touching_breaker(breaker_log):
    calls = []

    def chat(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "primary-model":
            raise ProviderRequestError("B2Bank v1 cannot represent multiple assistant tool calls")
        return {"content": "ok"}, {}

    message, _usage, used_model = _chat_with_fallback(chat)

    assert (message["content"], used_model) == ("ok", "fallback-model")
    assert calls == ["primary-model", "fallback-model"]
    assert breaker_log.failures == []


def test_background_model_unavailable_falls_back_without_retry_or_breaker(breaker_log):
    calls = []

    def chat(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "primary-model":
            raise _model_unavailable_error()
        return {"content": "ok"}, {}

    message, _usage, used_model = _chat_with_fallback(chat)

    assert (message["content"], used_model) == ("ok", "fallback-model")
    assert calls == ["primary-model", "fallback-model"]
    assert breaker_log.failures == []


# Routing only; the real validators' response matrix lives in
# test_background_response_validators_return_typed_outcomes.
@pytest.mark.parametrize(
    ("outcome", "expected_failure", "expected_success"),
    [
        (None, None, True),
        (InvalidResponse.rejected("bad response"), None, False),
        (InvalidResponse.incomplete("finish_reason=null"), LLMFailureCategory.INCOMPLETE, False),
        (ssl.SSLError("TLS handshake failed"), LLMFailureCategory.TRANSIENT, False),
        (socket.gaierror(-2, "name resolution failed"), LLMFailureCategory.TRANSIENT, False),
        (
            httpx.DecodingError("response decode failed", request=httpx.Request("POST", "https://llm.invalid")),
            LLMFailureCategory.TRANSIENT,
            False,
        ),
    ],
)
def test_background_routing_mutates_breaker_only_for_confirmed_outcomes(
    breaker_log, outcome, expected_failure, expected_success
):
    def chat(**_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return {"content": "ok"}, {}

    error = None
    try:
        _chat_with_fallback(
            chat,
            fallback_model="",
            response_error=lambda _msg: outcome if isinstance(outcome, InvalidResponse) else None,
        )
    except Exception as exc:  # the rejected/failure cases intentionally exhaust this one-model route
        error = exc

    assert (error is None) is expected_success
    assert [(model, category) for model, category, _error in breaker_log.failures] == (
        [] if expected_failure is None else [("primary-model", expected_failure)]
    )
    assert breaker_log.successes == (["primary-model"] if expected_success else [])


@pytest.mark.parametrize("timeout_point", ["semaphore", "before_provider"])
def test_background_local_deadline_timeouts_do_not_mutate_provider_breaker(monkeypatch, breaker_log, timeout_point):
    from new_core.coordination.interfaces import IConcurrencyBudget, Lease
    from ouroboros.llm import client as llm_client

    class Budget(IConcurrencyBudget):
        """Grants a slot, or never does — the two places a deadline can expire."""

        def acquire(self, budget, *, holder, ttl_sec, timeout_sec=None):
            if timeout_point == "semaphore":
                return None
            return Lease(budget=budget, holder=holder, token="t", expires_at=0.0)

        async def acquire_async(self, budget, *, holder, ttl_sec, timeout_sec=None):
            return self.acquire(budget, holder=holder, ttl_sec=ttl_sec, timeout_sec=timeout_sec)

        def release(self, lease):
            return None

        def limit(self, budget):
            return 1

        def in_use(self, budget):
            return 0

    class Adapter:
        def __init__(self):
            self.calls = []

        def chat(self, *_args, **_kwargs):
            self.calls.append((_args, _kwargs))
            raise AssertionError("provider must not be called after the caller deadline")

    adapter = Adapter()
    registry = type("Registry", (), {"resolve": lambda self, _model: (adapter, {})})()
    client = llm_client.LLMClient(registry=registry)
    # Stateful clock: the number of monotonic() calls is an implementation detail.
    clock = {"now": 100.0}

    def tick() -> float:
        now = clock["now"]
        clock["now"] += 0.5 if timeout_point == "semaphore" else 0.7
        return now

    monkeypatch.setattr(llm_client.time, "monotonic", tick)
    from new_core.coordination.registry import concurrency_budgets

    concurrency_budgets.initialize(Budget())

    with pytest.raises(RuntimeError, match="Background LLM attempts failed"):
        llm_client.chat_with_fallback(
            client,
            messages=[{"role": "user", "content": "hello"}],
            model="primary-model",
            fallback_model="",
            deadline=101.0,
            timeout_sec=30,
        )

    concurrency_budgets.reset_for_tests()
    assert adapter.calls == []
    assert breaker_log.failures == []
    assert breaker_log.successes == []
