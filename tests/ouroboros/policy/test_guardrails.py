"""GR Services /check-only monitoring: verdict parsing and the LLM middleware."""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from new_core.llm.types import LLMMessage, ToolCall, ToolCallFunction
from ouroboros.observability import aef_tracing
from ouroboros.policy.guardrails import Direction, GuardrailsClient, GuardrailsMiddleware, GuardrailVerdict
from tests.conftest import refresh_test_config
from tests.helpers.http_double import FakeHttpResponse
from tests.helpers.parametrize import parametrize_from_dict

CHECK_URL = "https://gr.example/check_only"
TRACE_ID = "7ee9b153-88aa-4f6b-ae66-44985ac81ead"
#: The other spelling a caller may send; it normalizes to `TRACE_ID` before reaching the wire.
PACKED = "7ee9b15388aa4f6bae6644985ac81ead"
OTHER = "463ac35c-9f64-13ad-4848-5a3953bb6124"


class _GrResponse(FakeHttpResponse):
    """Extends FakeHttpResponse with the guardrails default ALLOW response."""

    def __init__(self, *, status_code: int = 200, headers: dict[str, str] | None = None, payload: Any = None) -> None:
        super().__init__(payload=payload, status_code=status_code, headers=headers)

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("response is not JSON")
        return self._payload


class _GrClient:
    """Stands in for httpx.Client; answers ALLOW unless a response is queued."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.created: list[dict[str, Any]] = [kwargs]
        self.calls: list[dict[str, Any]] = []
        self.responses: list[_GrResponse | Exception] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _GrResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self.responses:
            return _GrResponse(headers={f"gr-decision-{headers['DIRECTION']}": "ALLOW"})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _content_response(content: str) -> _GrResponse:
    return _GrResponse(payload={"choices": [{"message": {"role": "assistant", "content": content}}]})


@pytest.fixture()
def fake_http():
    """A rig that hands out _GrClient instances and tracks them."""
    rig = SimpleNamespace(instances=[], pre_responses=[])

    def _next_client(**kwargs: Any) -> _GrClient:
        client = _GrClient(**kwargs)
        client.responses = rig.pre_responses
        rig.pre_responses = []
        rig.instances.append(client)
        return client

    rig._next_client = _next_client
    return rig


def _client(fake_http, **overrides: Any) -> GuardrailsClient:
    kwargs: dict[str, Any] = {
        "url": CHECK_URL,
        "agent_id": "CI12345678",
        "model": "GigaChat",
        "timeout": 30.0,
        "connect_timeout": 30.0,
        "verify": True,
        "client_factory": lambda **kw: fake_http._next_client(**kw),
    }
    kwargs.update(overrides)
    return GuardrailsClient(**kwargs)


def _check(client: GuardrailsClient, text: str, direction: Direction, trace_id: str = TRACE_ID) -> GuardrailVerdict:
    """A check as production makes it: inside the scope the task opened, never outside one."""
    with aef_tracing.trace_scope(trace_id):
        return client.check(text, direction)


def _checks(fake_http) -> list[tuple[str, str]]:
    """What reached GR: (text, direction) per request, in order."""
    return [
        (call["json"]["messages"][0]["content"], call["headers"]["DIRECTION"]) for call in fake_http.instances[-1].calls
    ]


def _sent_trace_ids(fake_http) -> list[str | None]:
    return [call["headers"].get(aef_tracing.TRACE_HEADER_NAME) for call in fake_http.instances[-1].calls]


@parametrize_from_dict(
    {
        "allow from header": {"response": _GrResponse(headers={"gr-decision-input": "ALLOW"}), "decision": "ALLOW"},
        "block from header": {"response": _GrResponse(headers={"gr-decision-input": "BLOCK"}), "decision": "BLOCK"},
        "header case does not matter": {
            "response": _GrResponse(headers={"GR-Decision-Input": "Block"}),
            "decision": "BLOCK",
        },
        "allow from body when headers are stripped": {"response": _content_response("ALLOW"), "decision": "ALLOW"},
        "block from body when headers are stripped": {"response": _content_response("BLOCK"), "decision": "BLOCK"},
    }
)
def test_check_reads_the_aggregate_decision(fake_http: Any, response: _GrResponse, decision: str) -> None:
    fake_http.pre_responses = [response]

    verdict = _check(_client(fake_http), "привет", "input")

    assert verdict == GuardrailVerdict(direction="input", decision=decision)


@parametrize_from_dict(
    {
        "http error status": {"response": _GrResponse(status_code=500)},
        "undecidable response": {"response": _content_response("что-то невнятное")},
        "network failure": {"response": httpx.ConnectError("connection refused")},
    }
)
def test_check_failure_becomes_error_verdict_with_reason(fake_http: Any, response: _GrResponse | Exception) -> None:
    fake_http.pre_responses = [response]

    verdict = _check(_client(fake_http), "привет", "input")

    assert verdict.decision == "ERROR"
    assert verdict.reason


def test_check_sends_the_agreed_contract(fake_http: Any) -> None:
    _check(_client(fake_http), "привет", "input")

    call = fake_http.instances[-1].calls[0]
    assert call["url"] == CHECK_URL
    assert call["headers"]["x-agent-id"] == "CI12345678"
    assert call["headers"][aef_tracing.TRACE_HEADER_NAME] == TRACE_ID
    assert call["headers"]["DIRECTION"] == "input"
    assert call["json"] == {
        "model": "GigaChat",
        "messages": [{"role": "user", "content": "привет"}],
        "stream": False,
    }


def test_output_check_wraps_text_as_assistant_message(fake_http: Any) -> None:
    verdict = _check(_client(fake_http), "готовый ответ", "output")

    assert verdict.decision == "ALLOW"
    assert fake_http.instances[-1].calls[0]["json"]["messages"] == [{"role": "assistant", "content": "готовый ответ"}]
    assert fake_http.instances[-1].calls[0]["headers"]["DIRECTION"] == "output"


def test_client_opens_one_http_client_with_its_timeouts_and_tls(fake_http: Any) -> None:
    client = _client(fake_http, verify=False, timeout=5.0, connect_timeout=2.0)

    _check(client, "привет", "input")
    _check(client, "ещё раз", "input")

    # An unreachable GR must fail on the short connect window, not wait out the read one.
    assert fake_http.pre_responses[:] or [inst.kwargs for inst in fake_http.instances] == [
        {"timeout": httpx.Timeout(5.0, connect=2.0), "verify": False}
    ]


def test_middleware_monitors_each_new_user_message_once(fake_http: Any) -> None:
    middleware = GuardrailsMiddleware(_client(fake_http))
    messages: list[dict[str, Any]] = [{"role": "system", "content": "prompt"}, {"role": "user", "content": "привет"}]

    middleware.before_call(messages)
    # A tool round adds no owner content: the same input must not be paid for twice.
    middleware.before_call([*messages, {"role": "assistant", "content": "думаю"}, {"role": "tool", "content": "{}"}])
    middleware.before_call([*messages, {"role": "user", "content": "ещё вопрос"}])

    assert _checks(fake_http) == [("привет", "input"), ("ещё вопрос", "input")]


def test_middleware_reads_the_owner_text_behind_a_tool_round(fake_http: Any) -> None:
    # The newest user text is not the last message once tool rounds follow it.
    GuardrailsMiddleware(_client(fake_http)).before_call(
        [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "привет"},
            {"role": "assistant", "content": "думаю"},
            {"role": "tool", "content": "{}"},
        ]
    )

    assert _checks(fake_http) == [("привет", "input")]


def test_middleware_monitors_an_answer_that_has_no_tool_calls(fake_http: Any) -> None:
    middleware = GuardrailsMiddleware(_client(fake_http))

    middleware.after_call(LLMMessage(content="последний ответ"))

    assert _checks(fake_http) == [("последний ответ", "output")]


def test_middleware_checks_an_unchanged_answer_once(fake_http: Any) -> None:
    middleware = GuardrailsMiddleware(_client(fake_http))

    middleware.after_call(LLMMessage(content="ответ"))
    middleware.after_call(LLMMessage(content="ответ"))

    assert _checks(fake_http) == [("ответ", "output")]


def test_middleware_does_not_monitor_intermediate_rounds(fake_http: Any) -> None:
    tool_call = ToolCall(id="1", type="function", function=ToolCallFunction(name="noop", arguments="{}"))
    middleware = GuardrailsMiddleware(_client(fake_http))

    middleware.after_call(LLMMessage(content="промежуточный", tool_calls=[tool_call]))
    middleware.after_call(None)

    assert _checks(fake_http) == []


def test_middleware_skips_blank_text(fake_http: Any) -> None:
    middleware = GuardrailsMiddleware(_client(fake_http))

    middleware.after_call(LLMMessage(content="   "))

    assert _checks(fake_http) == []


def test_middleware_sends_a_dashed_uuid_trace_id(fake_http: Any) -> None:
    with aef_tracing.trace_scope(PACKED):
        GuardrailsMiddleware(_client(fake_http)).before_call([{"role": "user", "content": "привет"}])

    # GR examples use the dashed UUID form, and it is the one spelling the agent puts on the wire
    # whichever spelling the caller sent — so a caller that sent no dashes gets them back here.
    trace_id = fake_http.instances[-1].calls[0]["headers"][aef_tracing.TRACE_HEADER_NAME]
    assert trace_id == TRACE_ID
    assert str(uuid.UUID(trace_id)) == trace_id


def test_a_check_with_no_identifier_active_omits_the_header(fake_http: Any) -> None:
    """A generated value would label the request with an operation nothing else ever names."""
    _client(fake_http).check("привет", "input")

    assert _sent_trace_ids(fake_http) == [None]


def test_one_client_sends_each_operations_own_identifier(fake_http: Any) -> None:
    """The client outlives the operation, so the identifier is read per request, never at
    construction: two operations over the same connection must not share the first one's."""
    client = _client(fake_http)

    _check(client, "привет", "input", TRACE_ID)
    _check(client, "ещё раз", "input", OTHER)

    assert _sent_trace_ids(fake_http) == [TRACE_ID, OTHER]


def test_middleware_survives_a_client_error_outside_httpx(fake_http: Any) -> None:
    # httpx.InvalidURL is not an httpx.HTTPError: a misconfigured URL must not
    # escape the monitor and take the task down with it.
    fake_http.pre_responses = [httpx.InvalidURL("malformed URL")]

    GuardrailsMiddleware(_client(fake_http)).before_call([{"role": "user", "content": "привет"}])


def test_middleware_logs_every_failed_check(fake_http: Any, caplog: pytest.LogCaptureFixture) -> None:
    unavailable = httpx.codes.BAD_GATEWAY
    fake_http.pre_responses = [_GrResponse(status_code=unavailable), _GrResponse(status_code=unavailable)]
    middleware = GuardrailsMiddleware(_client(fake_http))

    with caplog.at_level("WARNING", logger="ouroboros.policy.guardrails"):
        middleware.before_call([{"role": "user", "content": "привет"}])
        middleware.before_call([{"role": "user", "content": "ещё раз"}])

    assert len(caplog.records) == 2


def test_middleware_always_logs_a_block(fake_http: Any, caplog: pytest.LogCaptureFixture) -> None:
    fake_http.pre_responses = [
        _GrResponse(headers={"gr-decision-input": "BLOCK"}),
        _GrResponse(headers={"gr-decision-input": "BLOCK"}),
    ]
    middleware = GuardrailsMiddleware(_client(fake_http))

    with caplog.at_level("WARNING", logger="ouroboros.policy.guardrails"):
        middleware.before_call([{"role": "user", "content": "привет"}])
        middleware.before_call([{"role": "user", "content": "ещё раз"}])

    assert len(caplog.records) == 2


def test_an_unconfigured_agent_sends_no_identity_header(fake_http: Any) -> None:
    """Absent rather than empty, the rule every other outbound integration follows: an empty
    value passes a validator and identifies nobody."""
    _check(_client(fake_http, agent_id=""), "привет", "input")

    assert "x-agent-id" not in fake_http.instances[-1].calls[0]["headers"]


@pytest.fixture
def load_llm_middlewares(monkeypatch: pytest.MonkeyPatch):
    """Re-import the middleware list against a patched env, and undo it afterwards.

    The module builds the list at import time, so a test that changes ``GR_*`` has to
    reload it — and has to put back what the rest of the session imported.
    """
    snapshot = dict(os.environ)

    def _load(**env: str):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(importlib.import_module("ouroboros.policy.llm_middlewares"))

    yield _load

    os.environ.clear()
    os.environ.update(snapshot)
    refresh_test_config()
    module = sys.modules.get("ouroboros.policy.llm_middlewares")
    if module is not None:
        importlib.reload(module)


def test_llm_middlewares_are_empty_while_guardrails_are_off(load_llm_middlewares) -> None:
    module = load_llm_middlewares(GR_ENABLED="false")

    assert module.llm_middlewares == []


def test_llm_middlewares_hold_guardrails_when_enabled(load_llm_middlewares) -> None:
    module = load_llm_middlewares(GR_ENABLED="true", GR_AGENT_ID="CI12345678")

    assert [type(factory()) for factory in module.llm_middlewares] == [GuardrailsMiddleware]


def test_guardrails_fall_back_to_the_agents_own_identifier(load_llm_middlewares) -> None:
    """`GR_AGENT_ID` is empty by default, and GR rejects a check without one — so the deployment
    ends up setting the same identifier twice, or, as it did, sending an empty header."""
    module = load_llm_middlewares(
        GR_ENABLED="true",
        GR_AGENT_ID="",
        AGENT_SYSTEM_CONFIGURATION_AGENT_ID="CI0000123",
    )

    assert module._guardrails._agent_id == "CI0000123"


def test_the_guardrails_key_still_overrides_the_shared_one(load_llm_middlewares) -> None:
    """The two identifiers may legitimately differ; the specific key keeps winning."""
    module = load_llm_middlewares(
        GR_ENABLED="true",
        GR_AGENT_ID="CI12345678",
        AGENT_SYSTEM_CONFIGURATION_AGENT_ID="CI0000123",
    )

    assert module._guardrails._agent_id == "CI12345678"
