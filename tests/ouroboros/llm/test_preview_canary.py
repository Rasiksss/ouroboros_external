"""TN09: canary routing of a bounded share of requests to a preview model version."""

from __future__ import annotations

import json

import httpx
import pytest

from ouroboros.agent_events.interfaces import LlmUsageReported
from ouroboros.llm.preview_canary import (
    record_preview_failure,
    reset_preview_canary_for_tests,
    select_request_model,
)
from ouroboros.llm.providers.gigachat_b2bank import GigachatB2BankProvider
from ouroboros.storage.settings.store import reset_memory_settings_for_tests
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE, FakeCancellation

MAIN = "GigaChat-2-Max"
PREVIEW = "GigaChat-2-Max-preview"
LIGHT = "GigaChat-Lite"
FALLBACK = "GigaChat-2"


@pytest.fixture(autouse=True)
def _clean_canary_state():
    """Both halves of the state this module mutates: the counters and the settings cache."""
    reset_preview_canary_for_tests()
    reset_memory_settings_for_tests()
    yield
    reset_preview_canary_for_tests()
    reset_memory_settings_for_tests()


def _enable(monkeypatch, probability: str = "0.05", *, preview: str = PREVIEW) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL", f"gigachat-b2bank::{MAIN}")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", f"gigachat-b2bank::{LIGHT}")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", f"gigachat-b2bank::{FALLBACK}")
    monkeypatch.setenv("GIGACHAT_PREVIEW_ENABLED", "true")
    monkeypatch.setenv("GIGACHAT_PREVIEW_MODEL", preview)
    monkeypatch.setenv("GIGACHAT_PREVIEW_PROBABILITY", probability)
    reset_memory_settings_for_tests()


def _ref(model: str) -> str:
    return f"gigachat-b2bank::{model}" if model else ""


# --- how much traffic the check takes --------------------------------------


def test_disabled_by_default_never_selects_preview(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    monkeypatch.setenv("GIGACHAT_PREVIEW_ENABLED", "false")
    reset_memory_settings_for_tests()

    assert [select_request_model(MAIN) for _ in range(100)] == [MAIN] * 100


def test_enabled_with_zero_share_never_selects_preview(monkeypatch):
    _enable(monkeypatch, "0", preview=_ref(PREVIEW))

    assert [select_request_model(MAIN) for _ in range(100)] == [MAIN] * 100


def test_share_is_exact_and_never_exceeds_the_ceiling(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    picks = [select_request_model(MAIN) for _ in range(100)]

    # A counter, not a dice roll: exactly five of a hundred, in every window.
    assert picks.count(PREVIEW) == 5
    assert all(picks[:n].count(PREVIEW) <= n * 0.05 for n in range(1, 101))


def test_an_installed_build_that_is_itself_the_named_one_is_not_a_canary(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    assert [select_request_model(PREVIEW) for _ in range(100)] == [PREVIEW] * 100


def test_an_unusable_preview_name_is_reported_not_silently_ignored(monkeypatch, caplog):
    """A typo would otherwise read as a healthy 0% forever, with nothing in the logs."""
    _enable(monkeypatch, "0.05", preview="not a model reference")

    with caplog.at_level("WARNING"):
        picks = [select_request_model(MAIN) for _ in range(100)]

    assert picks == [MAIN] * 100
    warnings = [r.getMessage() for r in caplog.records if "Preview canary" in r.getMessage()]
    # Reported once, not once per request.
    assert len(warnings) == 1
    assert "GIGACHAT_PREVIEW_MODEL" in warnings[0]


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_the_returned_name_is_stripped_on_every_path(monkeypatch, enabled):
    """The adapter compares the answer against the model it passed in; a path that
    returns the raw value would send a doomed double request."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    monkeypatch.setenv("GIGACHAT_PREVIEW_ENABLED", enabled)
    reset_memory_settings_for_tests()

    assert select_request_model(f"  {MAIN}  ") == MAIN


# --- which requests stay out of it -----------------------------------------


@pytest.mark.parametrize("model", [LIGHT, FALLBACK, "GigaChat-Vision"])
def test_only_the_shadowed_lane_is_drawn(monkeypatch, model):
    """Fallback and vision must keep their own model: an incident is not the time to
    discover the preview build was never published."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    assert [select_request_model(model) for _ in range(100)] == [model] * 100


def test_a_caller_with_a_deadline_is_never_routed(monkeypatch):
    """A bounded call has no room for the fallback attempt, so it does not take part."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    assert [select_request_model(MAIN, bounded=True) for _ in range(100)] == [MAIN] * 100


# --- degradation on failure ------------------------------------------------


def test_first_failure_halves_the_share(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    record_preview_failure(PREVIEW, request_id="req-1", reason="provider_outage")

    # Halved to 2.5%: the counter now yields half as many picks.
    assert [select_request_model(MAIN) for _ in range(100)].count(PREVIEW) == 2


def test_second_failure_disables_the_check_until_restart(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    record_preview_failure(PREVIEW, request_id="req-1", reason="provider_outage")
    record_preview_failure(PREVIEW, request_id="req-2", reason="provider_outage")

    assert [select_request_model(MAIN) for _ in range(100)] == [MAIN] * 100


def test_failure_is_journalled_with_model_request_and_reason(monkeypatch, caplog):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    with caplog.at_level("WARNING"):
        record_preview_failure(PREVIEW, request_id="req-42", reason="provider_outage")

    record = caplog.records[-1].getMessage()
    assert PREVIEW in record
    assert "req-42" in record
    assert "provider_outage" in record


# --- wiring into the B2Bank adapter ----------------------------------------


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://b2bank.test")
            response = httpx.Response(self.status_code, request=request, json=self._payload)
            raise httpx.HTTPStatusError(f"status {self.status_code}", request=request, response=response)

    def json(self):
        return self._payload

    @property
    def headers(self):
        return {}

    @property
    def text(self):
        return json.dumps(self._payload)


class _FakeClient:
    """Records what actually went on the wire, one entry per HTTP attempt."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.models: list[str] = []
        self.request_ids: list[str] = []
        self.timeouts: list[object] = []

    def post(self, _url, **kwargs):
        self.models.append(kwargs["json"]["model"])
        self.request_ids.append(kwargs["headers"]["X-Request-ID"])
        self.timeouts.append(kwargs.get("timeout"))
        return self._responses.pop(0) if self._responses else _answer()


def _answer(content="ok", *, usage=None):
    message = {"role": "assistant", "content": content}
    return _FakeResponse({"choices": [{"message": message}], "usage": usage or {}})


def _target():
    return {
        "resolved_model": MAIN,
        # What resolve_target() puts there: usage is reported in the prefixed form.
        "usage_model": f"gigachat-b2bank/{MAIN}",
        "base_url": "https://b2bank.test/v1",
        "verify_ssl": False,
        "chat_api_version": "v1",
    }


def _chat(monkeypatch, responses, *, selects=PREVIEW, client=None, **chat_kwargs):
    client = client or _FakeClient(responses)
    if selects is not None:
        monkeypatch.setattr(
            "ouroboros.llm.providers.gigachat_b2bank.select_request_model",
            lambda model, **_kwargs: selects,
        )
    result = GigachatB2BankProvider(client_factory=lambda **_kw: client).chat(
        _target(),
        [{"role": "user", "content": "hello"}],
        tools=None,
        reasoning_effort="none",
        max_tokens=32,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
        **chat_kwargs,
    )
    return client, result


def test_a_drawn_request_asks_the_preview_version_and_says_so_in_usage(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    client, (message, usage) = _chat(monkeypatch, [_answer("preview answer")])

    assert client.models == [PREVIEW]
    assert message["content"] == "preview answer"
    # Events read the answering version from here; nothing above the provider is involved.
    assert usage["resolved_model"] == f"gigachat-b2bank/{PREVIEW}"


def test_an_undrawn_request_is_sent_once_to_the_installed_version(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    client, (_message, usage) = _chat(monkeypatch, [_answer()], selects=MAIN)

    assert client.models == [MAIN]
    assert usage["resolved_model"] == f"gigachat-b2bank/{MAIN}"


def test_a_failing_preview_falls_back_to_the_installed_version(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    client, (message, usage) = _chat(
        monkeypatch,
        [_FakeResponse({"error": "boom"}, status_code=503), _answer("installed answer")],
    )

    assert client.models == [PREVIEW, MAIN]
    assert message["content"] == "installed answer"
    assert usage["resolved_model"] == f"gigachat-b2bank/{MAIN}"
    # The failure was charged: the share is halved.
    assert [select_request_model(MAIN) for _ in range(100)].count(PREVIEW) == 2


def test_an_empty_preview_answer_counts_as_a_failure(monkeypatch):
    """No text and no tool call is a failed round for the runtime, not an answer."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    client, (message, usage) = _chat(
        monkeypatch,
        [
            _answer("", usage={"prompt_tokens": 100}),
            _answer("installed answer", usage={"prompt_tokens": 120}),
        ],
    )

    assert client.models == [PREVIEW, MAIN]
    assert message["content"] == "installed answer"
    # The discarded attempt burned real tokens; the report must not hide them.
    assert usage["prompt_tokens"] == 220
    assert [select_request_model(MAIN) for _ in range(100)].count(PREVIEW) == 2


def test_tokens_of_a_preview_body_that_fails_to_parse_are_still_counted(monkeypatch):
    """HTTP 200 with a malformed body: the gateway billed for it either way."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    _client, (_message, usage) = _chat(
        monkeypatch,
        [
            _FakeResponse({"choices": [], "usage": {"prompt_tokens": 100}}),
            _answer("installed answer", usage={"prompt_tokens": 120}),
        ],
    )

    assert usage["prompt_tokens"] == 220


def test_a_fault_that_kills_both_versions_is_not_charged_to_preview(monkeypatch):
    """A bad request or a provider outage is not the version's fault."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    with pytest.raises(httpx.HTTPStatusError):
        _chat(monkeypatch, [_FakeResponse({}, status_code=500), _FakeResponse({}, status_code=500)])

    assert [select_request_model(MAIN) for _ in range(100)].count(PREVIEW) == 5


def test_a_disabled_preview_build_falls_back_instead_of_stopping_the_task(monkeypatch):
    """TN08 turns a 403 'temporarily unavailable' into a stop. For the version under test
    that is precisely the answer the check is looking for, not a reason to end the run."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    disabled = _FakeResponse({"message": "Service is temporarily unavailable"}, status_code=403)

    client, (message, _usage) = _chat(monkeypatch, [disabled, _answer("installed answer")])

    assert client.models == [PREVIEW, MAIN]
    assert message["content"] == "installed answer"
    assert [select_request_model(MAIN) for _ in range(100)].count(PREVIEW) == 2


def test_a_disabled_installed_build_still_stops_the_task(monkeypatch):
    from ouroboros.llm.providers.base import ModelUnavailableError

    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    disabled = _FakeResponse({"message": "Service is temporarily unavailable"}, status_code=403)

    with pytest.raises(ModelUnavailableError):
        _chat(monkeypatch, [_FakeResponse({}, status_code=503), disabled])


def test_an_empty_installed_answer_is_not_charged_to_preview_either(monkeypatch):
    """A provider answering nothing to everyone must not spend the degradation budget."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    _chat(monkeypatch, [_answer(""), _answer("")])

    assert [select_request_model(MAIN) for _ in range(100)].count(PREVIEW) == 5


def test_each_attempt_is_a_distinct_request_to_the_gateway(monkeypatch):
    """One id for two requests would make the retry look like a duplicate and would
    collapse the two p95 log lines into one."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    client, _result = _chat(
        monkeypatch,
        [_FakeResponse({}, status_code=503), _answer("installed answer")],
    )

    assert len(set(client.request_ids)) == 2


def test_both_attempts_share_one_read_budget(monkeypatch):
    """Capping only the probe would leave the retry on the client default and make a
    drawn request wait 1.5x the configured envelope."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    monkeypatch.setenv("OUROBOROS_LLM_HTTP_READ_TIMEOUT_SEC", "100")
    reset_memory_settings_for_tests()

    client, _result = _chat(
        monkeypatch,
        [_FakeResponse({}, status_code=503), _answer("installed answer")],
    )

    assert [timeout.read for timeout in client.timeouts] == [50.0, 50.0]


def test_an_undrawn_request_keeps_the_callers_own_timeout(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    client, _result = _chat(monkeypatch, [_answer()], selects=MAIN)

    assert client.timeouts == [None]


# --- the adapter feeds the selector, not a stub ----------------------------


def test_the_adapter_draws_through_the_real_counter(monkeypatch):
    """No stubbed selector: the twentieth request of a 5% share is the drawn one."""
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    client = _FakeClient([])

    for _ in range(20):
        _chat(monkeypatch, [], selects=None, client=client)

    assert client.models[:19] == [MAIN] * 19
    assert client.models[19] == PREVIEW


def test_the_adapter_marks_a_deadline_caller_as_bounded(monkeypatch):
    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    client = _FakeClient([])

    for _ in range(20):
        _chat(monkeypatch, [], selects=None, client=client, read_timeout_sec=5.0)

    assert client.models == [MAIN] * 20


# --- reporting -------------------------------------------------------------


def test_events_report_the_version_that_actually_answered(tmp_path, monkeypatch):
    """The provider names it in usage and the loop passes that through — the reason
    nothing above the adapter needs to know about the check."""

    from ouroboros.agent import loop_llm_call as loop_module

    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    recorded: list[dict] = []
    monkeypatch.setattr(
        loop_module,
        "record_event",
        lambda _stream, event_type, payload, **_kw: recorded.append({"type": event_type, **payload}),
    )

    class _PreviewAnsweredLLM:
        def chat(self, **_kwargs):
            return {"content": "ok"}, {
                "resolved_model": f"gigachat-b2bank/{PREVIEW}",
                "provider": "gigachat-b2bank",
            }

    events = RecordingAgentEvents()
    loop_module.call_llm_with_retry(
        _PreviewAnsweredLLM(),
        [{"role": "user", "content": "hi"}],
        f"gigachat-b2bank::{MAIN}",
        None,
        "medium",
        1,
        tmp_path,
        "task-1",
        1,
        events,
        {},
        "task",
        False,
        scope=TEST_SCOPE,
        cancellation=FakeCancellation(),
        audit_event_repository=sql_audit_event_repository(),
    )

    rounds = [event for event in recorded if event["type"] == "llm_round"]
    assert [event["model"] for event in rounds] == [f"gigachat-b2bank/{PREVIEW}"]
    assert rounds[0]["model_category"] == "main"
    usage_events = events.of_type(LlmUsageReported)
    assert [event.model for event in usage_events] == [f"gigachat-b2bank/{PREVIEW}"]


def test_preview_traffic_stays_in_the_role_bucket_it_shadows(monkeypatch):
    """Events carry the usage-model shape, so the bucket is decided on that form."""
    from ouroboros.observability.pricing import infer_model_category

    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))

    assert infer_model_category(f"gigachat-b2bank/{PREVIEW}") == "main"
    assert infer_model_category(f"gigachat-b2bank/{MAIN}") == "main"
    assert infer_model_category(f"gigachat-b2bank/{LIGHT}") == "light"


def test_a_promoted_build_reports_the_lane_it_now_serves(monkeypatch):
    """After a rollback the name may linger in the environment while the build itself is
    promoted into another lane; the real lane wins."""
    from ouroboros.observability.pricing import infer_model_category

    _enable(monkeypatch, "0.05", preview=_ref(PREVIEW))
    monkeypatch.setenv("GIGACHAT_PREVIEW_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", _ref(PREVIEW))
    reset_memory_settings_for_tests()

    assert infer_model_category(f"gigachat-b2bank/{PREVIEW}") == "fallback"
