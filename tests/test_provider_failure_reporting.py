import json
import socket
import ssl
from types import SimpleNamespace

import httpx
import pytest

from new_core.llm.types import LLMMessage
from new_core.loop._helpers import provider_failure_hint
from ouroboros.agent.loop_llm_call import call_llm_with_retry
from ouroboros.llm.circuit_breaker import (
    CircuitBreaker,
    LLMFailureCategory,
    ProviderTarget,
    classify_failure,
    provider_failure_category,
    reset_circuit_breakers,
)
from ouroboros.llm.client import InvalidResponse
from ouroboros.llm.providers.base import ProviderRequestError, ProviderResponseError


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


class _FailingLLM:
    def chat(self, **kwargs):
        raise RuntimeError("AuthenticationError('401 invalid_api_key')")


class _SuccessfulLLM:
    def chat(self, **kwargs):
        return {"content": "ok"}, {"provider": "anthropic", "resolved_model": "anthropic/claude-sonnet-4-6"}


def test_call_llm_with_retry_records_last_error(tmp_path):
    usage = {}

    msg = call_llm_with_retry(
        _FailingLLM(),
        [{"role": "user", "content": "hi"}],
        "openai::gpt-5.5",
        None,
        "medium",
        1,
        tmp_path,
        "task-1",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg is None
    assert "invalid_api_key" in usage["_last_llm_error"]


def test_call_llm_with_retry_clears_stale_last_error_on_success(tmp_path):
    usage = {"_last_llm_error": "old error"}

    msg = call_llm_with_retry(
        _SuccessfulLLM(),
        [{"role": "user", "content": "hi"}],
        "anthropic::claude-sonnet-4-6",
        None,
        "medium",
        1,
        tmp_path,
        "task-2",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg == LLMMessage(content="ok")
    assert "_last_llm_error" not in usage


def test_provider_failure_hint_formats_detail():
    hint = provider_failure_hint({"_last_llm_error": "  AuthenticationError('401 invalid_api_key')  "})

    assert hint == " Last provider error: AuthenticationError('401 invalid_api_key')"


def test_provider_failure_hint_empty_without_error():
    assert provider_failure_hint({}) == ""


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
    from ouroboros.llm import client as llm_client

    class Semaphore:
        def acquire(self, **_kwargs):
            return timeout_point != "semaphore"

        def release(self):
            return None

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
    monkeypatch.setattr(llm_client, "_llm_semaphore", Semaphore)

    with pytest.raises(RuntimeError, match="Background LLM attempts failed"):
        llm_client.chat_with_fallback(
            client,
            messages=[{"role": "user", "content": "hello"}],
            model="primary-model",
            fallback_model="",
            deadline=101.0,
            timeout_sec=30,
        )

    assert adapter.calls == []
    assert breaker_log.failures == []
    assert breaker_log.successes == []
