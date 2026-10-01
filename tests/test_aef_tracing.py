from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import types
from contextlib import contextmanager
from typing import Any

from new_core.llm.types import ToolCall, ToolCallFunction
from new_core.tools.interfaces import IToolProvider
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from tests._shared import FakeCancellation, make_catalog


def _aef_env() -> dict[str, str]:
    return {
        "AEF_TRACING_ENABLED": "true",
        "AEF_TRACING_KAFKA_TOPIC": "agent.traces",
        "AEF_TRACING_SDK_LIST": json.dumps(
            [
                {
                    "type": "development",
                    "provider": "ouroboros",
                    "version": "1.0.0",
                    "stack": "python",
                }
            ]
        ),
        "AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9093, kafka-2:9093",
        "AGENT_SYSTEM_CONFIGURATION_AGENT_ID": "CI0000123",
        "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID": "dev-cluster",
        "AGENT_SYSTEM_CONFIGURATION_NAMESPACE": "dev-ns",
        "AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE": "https://example.invalid/ouroboros.zip",
    }


def test_trace_context_validates_propagates_and_restores_nested_scope():
    from ouroboros.observability import aef_tracing as mod

    mod.reset_aef_tracing_for_tests()
    outer_id = "463ac35c9f6413ad48485a3953bb6124"
    inner_id = "463ac35c9f6413ad48485a3953bb6125"

    with mod.trace_scope(outer_id) as active_outer:
        assert active_outer == outer_id
        assert mod.current_trace_id() == outer_id
        assert mod.with_trace_header({"Accept": "application/json"}) == {
            "Accept": "application/json",
            "X-Trace-Id": outer_id,
        }
        with mod.trace_scope(inner_id):
            assert mod.current_trace_id() == inner_id
        assert mod.current_trace_id() == outer_id

    assert mod.current_trace_id() == ""
    generated = mod.ensure_trace_id("invalid")
    assert len(generated) == 32
    int(generated, 16)


def test_trace_scope_updates_optional_framework_context(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    class FrameworkContext:
        def __init__(self):
            self.values: list[str] = []

        def set(self, value: str) -> None:
            self.values.append(value)

    framework_context = FrameworkContext()
    monkeypatch.setattr(mod, "_FRAMEWORK_TRACE_CONTEXT", framework_context, raising=False)

    trace_id = "463ac35c9f6413ad48485a3953bb6124"
    with mod.trace_scope(trace_id):
        assert framework_context.values[-1] == trace_id

    assert framework_context.values[-1] == ""


def test_trace_id_is_extracted_case_insensitively_from_headers():
    from ouroboros.observability import aef_tracing as mod

    trace_id = "463ac35c9f6413ad48485a3953bb6124"

    assert mod.trace_id_from_headers({"x-trace-id": trace_id}) == trace_id
    assert mod.trace_id_from_headers({"X-TRACE-ID": "invalid"}) == ""


def test_build_aef_tracing_config_reuses_agent_configuration_identity_and_kafka():
    from ouroboros.observability import aef_tracing as mod

    config = mod.build_aef_tracing_config(env=_aef_env())

    assert config.enabled is True
    assert config.topic == "agent.traces"
    assert config.bootstrap_servers == ["kafka-1:9093", "kafka-2:9093"]
    assert config.sdk_contract_version == "1.5"
    assert config.auto_instrumentation_enabled is False
    assert config.agent_headers == {
        "agent-id": "CI0000123",
        "cluster-id": "dev-cluster",
        "namespace": "dev-ns",
        "distributive": "https://example.invalid/ouroboros.zip",
    }
    assert config.sdk_list == [
        {
            "type": "development",
            "provider": "ouroboros",
            "version": "1.0.0",
            "stack": "python",
            "contract_version": "1.5",
        }
    ]


def test_aef_tracing_config_allows_sdk_contract_version_override():
    from ouroboros.observability import aef_tracing as mod

    env = _aef_env()
    env["AEF_TRACING_SDK_CONTRACT_VERSION"] = "1.6"

    config = mod.build_aef_tracing_config(env=env)

    assert config.sdk_contract_version == "1.6"
    assert config.sdk_list == [
        {
            "type": "development",
            "provider": "ouroboros",
            "version": "1.0.0",
            "stack": "python",
            "contract_version": "1.6",
        }
    ]


def test_aef_tracing_config_applies_python_sdk_env_aliases():
    from ouroboros.observability import aef_tracing as mod

    config = mod.build_aef_tracing_config(env=_aef_env())
    target_env: dict[str, str] = {}

    mod.apply_aef_sdk_env(config, environ=target_env)

    assert target_env == {
        "AGENT_ID": "CI0000123",
        "CLUSTER_ID": "dev-cluster",
        "POD_NAMESPACE": "dev-ns",
        "DISTRIBUTIVE": "https://example.invalid/ouroboros.zip",
    }


def test_configure_aef_tracing_builds_kafka_sender_with_sdk_list(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    mod.reset_aef_tracing_for_tests()
    target_env: dict[str, str] = {}
    providers: list[Any] = []
    uninstrument_calls: list[str] = []

    class FakeSDKEntry:
        model_fields = {
            "type": object(),
            "provider": object(),
            "version": object(),
            "stack": object(),
            "contract_version": object(),
        }

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSender:
        instances: list[FakeSender] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.instances.append(self)

    class FakeExporter:
        instances: list[FakeExporter] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.instances.append(self)

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class FakeProvider:
        def __init__(self):
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    fake_sdk = types.SimpleNamespace(
        AEFKafkaSender=FakeSender,
        SDKEntry=FakeSDKEntry,
        AEFProtobufSenderExporter=FakeExporter,
        AEFBatchSpanProcessor=FakeProcessor,
        AEFTracerProvider=FakeProvider,
        uninstrument=lambda: uninstrument_calls.append("uninstrument"),
    )
    monkeypatch.setattr(mod, "_import_aef_sdk", lambda: fake_sdk)
    monkeypatch.setattr(mod, "_set_tracer_provider", providers.append)

    assert mod.configure_aef_tracing(env=_aef_env(), environ=target_env) is True

    assert target_env["AGENT_ID"] == "CI0000123"
    assert providers and providers[0].processors
    assert uninstrument_calls == ["uninstrument"]
    sender_kwargs = FakeSender.instances[0].kwargs
    assert sender_kwargs["outbox_topic"] == "agent.traces"
    assert sender_kwargs["kafka_producer_config"] == {"bootstrap_servers": ["kafka-1:9093", "kafka-2:9093"]}
    assert sender_kwargs["headers"] == {
        "agent-id": "CI0000123",
        "cluster-id": "dev-cluster",
        "namespace": "dev-ns",
        "distributive": "https://example.invalid/ouroboros.zip",
    }
    assert [entry.kwargs for entry in sender_kwargs["sdk_list_headers"]] == [
        {
            "type": "development",
            "provider": "ouroboros",
            "version": "1.0.0",
            "stack": "python",
            "contract_version": "1.5",
        }
    ]
    assert FakeExporter.instances and FakeExporter.instances[0].kwargs == {"senders": FakeSender.instances}


def test_configure_aef_tracing_omits_unsupported_sdk_contract_version(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    mod.reset_aef_tracing_for_tests()

    class FakeSDKEntry:
        model_fields = {
            "type": object(),
            "provider": object(),
            "version": object(),
            "stack": object(),
        }

        def __init__(self, **kwargs):
            assert "contract_version" not in kwargs
            self.kwargs = kwargs

    class FakeSender:
        instances: list[FakeSender] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.instances.append(self)

    class FakeExporter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class FakeProvider:
        def add_span_processor(self, processor):
            self.processor = processor

    fake_sdk = types.SimpleNamespace(
        AEFKafkaSender=FakeSender,
        SDKEntry=FakeSDKEntry,
        AEFProtobufSenderExporter=FakeExporter,
        AEFBatchSpanProcessor=FakeProcessor,
        AEFTracerProvider=FakeProvider,
        uninstrument=lambda: None,
    )
    monkeypatch.setattr(mod, "_import_aef_sdk", lambda: fake_sdk)
    monkeypatch.setattr(mod, "_set_tracer_provider", lambda provider: None)

    assert mod.configure_aef_tracing(env=_aef_env(), environ={}) is True
    sdk_entries = FakeSender.instances[0].kwargs["sdk_list_headers"]
    assert [entry.kwargs for entry in sdk_entries] == [
        {
            "type": "development",
            "provider": "ouroboros",
            "version": "1.0.0",
            "stack": "python",
        }
    ]


def test_configure_aef_tracing_can_keep_sdk_auto_instrumentation(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    mod.reset_aef_tracing_for_tests()
    uninstrument_calls: list[str] = []

    class FakeSender:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeExporter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class FakeProvider:
        def add_span_processor(self, processor):
            self.processor = processor

    fake_sdk = types.SimpleNamespace(
        AEFKafkaSender=FakeSender,
        SDKEntry=lambda **kwargs: types.SimpleNamespace(kwargs=kwargs),
        AEFProtobufSenderExporter=FakeExporter,
        AEFBatchSpanProcessor=FakeProcessor,
        AEFTracerProvider=FakeProvider,
        uninstrument=lambda: uninstrument_calls.append("uninstrument"),
    )
    env = _aef_env()
    env["AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED"] = "true"
    monkeypatch.setattr(mod, "_import_aef_sdk", lambda: fake_sdk)
    monkeypatch.setattr(mod, "_set_tracer_provider", lambda provider: None)

    assert mod.configure_aef_tracing(env=env, environ={}) is True
    assert uninstrument_calls == []


def test_configure_aef_tracing_allows_empty_sdk_list(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    mod.reset_aef_tracing_for_tests()

    class FakeSender:
        instances: list[FakeSender] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.instances.append(self)

    class FakeExporter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class FakeProvider:
        def add_span_processor(self, processor):
            self.processor = processor

    fake_sdk = types.SimpleNamespace(
        AEFKafkaSender=FakeSender,
        SDKEntry=lambda **kwargs: types.SimpleNamespace(kwargs=kwargs),
        AEFProtobufSenderExporter=FakeExporter,
        AEFBatchSpanProcessor=FakeProcessor,
        AEFTracerProvider=FakeProvider,
        uninstrument=lambda: None,
    )
    env = _aef_env()
    env["AEF_TRACING_SDK_LIST"] = ""
    monkeypatch.setattr(mod, "_import_aef_sdk", lambda: fake_sdk)
    monkeypatch.setattr(mod, "_set_tracer_provider", lambda provider: None)

    assert mod.configure_aef_tracing(env=env, environ={}) is True
    assert "sdk_list_headers" not in FakeSender.instances[0].kwargs


def test_aef_span_helpers_mask_payloads_before_sdk_and_controller(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    mod.reset_aef_tracing_for_tests()
    for key, value in _aef_env().items():
        monkeypatch.setenv(key, value)
    started: list[dict[str, Any]] = []
    controller_calls: list[dict[str, Any]] = []

    class FakeSDKEntry:
        model_fields = {
            "type": object(),
            "provider": object(),
            "version": object(),
            "stack": object(),
            "contract_version": object(),
        }

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSender:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeExporter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class FakeProvider:
        def add_span_processor(self, processor):
            self.processor = processor

    class FakeController:
        def add_output_result(self, *, output):
            controller_calls.append({"output": output})

        def add_giga_llm_result(self, *, response_headers, response_body):
            controller_calls.append({"response_headers": response_headers, "response_body": response_body})

        def add_response(self, *, headers, body, http_code=200):
            controller_calls.append({"headers": headers, "body": body, "http_code": http_code})

    @contextmanager
    def fake_agent_start(**kwargs):
        started.append(kwargs)
        yield FakeController()

    fake_sdk = types.SimpleNamespace(
        AEFKafkaSender=FakeSender,
        SDKEntry=FakeSDKEntry,
        AEFProtobufSenderExporter=FakeExporter,
        AEFBatchSpanProcessor=FakeProcessor,
        AEFTracerProvider=FakeProvider,
        aef_agent_start=fake_agent_start,
        aef_input_request=fake_agent_start,
        aef_llm_request=fake_agent_start,
        aef_output_request=fake_agent_start,
        aef_tool_run=fake_agent_start,
        uninstrument=lambda: None,
    )

    monkeypatch.setattr(mod, "_import_aef_sdk", lambda: fake_sdk)
    monkeypatch.setattr(mod, "_set_tracer_provider", lambda provider: None)

    with mod.agent_start(input={"text": "secret"}) as span:
        span.add_output_result(output={"answer": "secret"})

    with mod.input_request(
        headers={"Authorization": "Bearer secret"},
        body={"message": "secret"},
        path="/a2a",
        method="POST",
    ) as span:
        span.add_response(headers={"content-type": "application/json"}, body={"answer": "secret"}, http_code=200)

    with mod.llm_request(
        request_headers={"Authorization": "Bearer secret"},
        request_body={"model": "model-a", "messages": [{"role": "user", "content": "secret"}]},
    ) as span:
        span.add_giga_llm_result(
            response_headers={"content-type": "application/json"},
            response_body=mod.mask_llm_response_payload(
                "model-a",
                {"role": "assistant", "content": "secret"},
                {"prompt_tokens": 1, "completion_tokens": 2},
            ),
        )

    assert started[0]["input"] == "***"
    assert controller_calls[0] == {"output": "***"}
    assert started[1]["headers"] == {"Authorization": "***"}
    assert started[1]["body"] == "***"
    assert controller_calls[1] == {
        "headers": {"content-type": "***"},
        "body": "***",
        "http_code": 200,
    }
    assert started[2]["request_headers"] == {"Authorization": "***"}
    assert started[2]["request_body"] == {
        "model": "model-a",
        "messages": [{"role": "user", "content": "***"}],
    }
    assert controller_calls[2]["response_headers"] == {"content-type": "***"}
    assert controller_calls[2]["response_body"]["choices"][0]["message"]["content"] == "***"


def test_set_session_id_updates_aef_contextvar(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    calls: list[str] = []
    fake_span_processors = types.SimpleNamespace(
        session_id_cvar=types.SimpleNamespace(set=lambda value: calls.append(value))
    )
    monkeypatch.setitem(sys.modules, "aef_tracing.span_processors", fake_span_processors)

    mod.set_session_id("session-123")

    assert calls == ["session-123"]


def test_set_session_id_noops_when_sdk_missing(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    monkeypatch.delitem(sys.modules, "aef_tracing.span_processors", raising=False)

    mod.set_session_id("session-123")


def test_app_settings_include_aef_tracing_keys(monkeypatch):
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.storage.settings.store import apply_settings_to_env

    sdk_list = '[{"type":"development","provider":"ouroboros","version":"1.0.0","stack":"python"}]'
    settings = AppSettings(
        AEF_TRACING_ENABLED=True,
        AEF_TRACING_KAFKA_TOPIC="agent.traces",
        AEF_TRACING_SDK_LIST=sdk_list,
        AEF_TRACING_SDK_CONTRACT_VERSION="1.6",
        AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED=True,
    )

    assert settings.AEF_TRACING_ENABLED is True
    assert settings.AEF_TRACING_KAFKA_TOPIC == "agent.traces"
    assert sdk_list == settings.AEF_TRACING_SDK_LIST
    assert settings.AEF_TRACING_SDK_CONTRACT_VERSION == "1.6"
    assert settings.AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED is True

    monkeypatch.delenv("AEF_TRACING_ENABLED", raising=False)
    monkeypatch.delenv("AEF_TRACING_KAFKA_TOPIC", raising=False)
    monkeypatch.delenv("AEF_TRACING_SDK_LIST", raising=False)
    monkeypatch.delenv("AEF_TRACING_SDK_CONTRACT_VERSION", raising=False)
    monkeypatch.delenv("AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED", raising=False)
    apply_settings_to_env(settings)

    assert os.environ["AEF_TRACING_ENABLED"] == "True"
    assert os.environ["AEF_TRACING_KAFKA_TOPIC"] == "agent.traces"
    assert os.environ["AEF_TRACING_SDK_LIST"] == sdk_list
    assert os.environ["AEF_TRACING_SDK_CONTRACT_VERSION"] == "1.6"
    assert os.environ["AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED"] == "True"


def test_tool_trace_payload_flag_projects_to_subprocess_env(monkeypatch):
    # Both directions matter: True must reach worker processes, and a stale
    # true in the environment must be removed when the setting is off.
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.storage.settings.store import apply_settings_to_env

    monkeypatch.delenv("AEF_TOOL_TRACE_PAYLOAD_ENABLED", raising=False)
    apply_settings_to_env(AppSettings(AEF_TOOL_TRACE_PAYLOAD_ENABLED=True))
    assert os.environ["AEF_TOOL_TRACE_PAYLOAD_ENABLED"] == "True"

    # Off projects the string "False" (not removal), so a stale "True" from a
    # debug session is overwritten; the schema coerces it back to a bool.
    apply_settings_to_env(AppSettings(AEF_TOOL_TRACE_PAYLOAD_ENABLED=False))
    assert os.environ["AEF_TOOL_TRACE_PAYLOAD_ENABLED"] == "False"


def test_agent_handle_task_records_agent_start_output(monkeypatch, tmp_path):
    from ouroboros.agent import core as mod

    spans: list[tuple[str, Any]] = []

    class FakeAgentSpan:
        def add_output_result(self, *, output):
            spans.append(("output", output))

    @contextmanager
    def fake_agent_start(*, input):
        spans.append(("start", input))
        yield FakeAgentSpan()

    agent = object.__new__(mod.OuroborosAgent)
    agent.env = mod.Env(repo_dir=tmp_path, drive_root=tmp_path)
    agent._pending_events = []
    agent._event_queue = None
    agent._current_chat_id = None
    agent._current_task_type = None
    agent._current_task_id = None
    agent._incoming_messages = queue.Queue()
    agent._busy = False
    agent._last_progress_ts = 0.0
    agent._task_started_ts = 0.0
    agent.llm = object()
    agent.memory = object()

    monkeypatch.setattr(mod.aef_tracing, "agent_start", fake_agent_start)
    monkeypatch.setattr(agent, "_emit_live_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_start_task_heartbeat_loop", lambda task_id: None)
    prepared_routing = []

    def prepare_task_context(task, task_workspace=None):
        del task, task_workspace
        prepared_routing.append((agent._current_chat_id, agent._current_task_source, agent._current_transport))
        return object(), [], {}

    monkeypatch.setattr(agent, "_prepare_task_context", prepare_task_context)
    monkeypatch.setattr(agent, "_emit_progress", lambda text: None)

    # handle_task is now scaffolding around a single graph invoke: build_task_run
    # yields the per-task context/state, and the graph publishes via emit_result.
    # Stand in a fake graph whose invoke drives prepare_context then the emit node,
    # so the span/routing/return assertions exercise only that scaffolding.
    class FakeEvents:
        def emit_result(self, *, text, usage, trace, tool_ctx):
            agent._pending_events.append({"type": "done"})

    graph_ctx = types.SimpleNamespace(config=types.SimpleNamespace(max_rounds=200), events=FakeEvents())
    state = types.SimpleNamespace(
        task=None, task_workspace=None, final_content=None, accumulated_usage={}, llm_trace={}, tool_ctx=None
    )

    def fake_build_task_run(ag, tk, *, task_workspace, start_time, drive_logs, cancellation):
        state.task = tk
        state.task_workspace = task_workspace
        return graph_ctx, state

    def fake_invoke(st, ctx, *, max_steps):
        ag_ctx, _messages, _cap = agent._prepare_task_context(st.task, st.task_workspace)
        st.tool_ctx = ag_ctx
        st.final_content = "agent answer"
        ctx.events.emit_result(text=st.final_content, usage=st.accumulated_usage, trace=st.llm_trace, tool_ctx=ag_ctx)

    agent._task_graph = types.SimpleNamespace(invoke=fake_invoke)
    monkeypatch.setattr(mod, "build_task_run", fake_build_task_run)
    events = agent.handle_task(
        {
            "id": "task-1",
            "type": "chat",
            "text": "hello",
            "chat_id": 0,
            "source": "browser",
            "transport": {"source_message_id": "message-1"},
            "_is_direct_chat": True,
        },
        cancellation=FakeCancellation(),
    )

    assert events == [{"type": "done"}]
    assert prepared_routing == [(0, "browser", {"source_message_id": "message-1"})]
    assert spans[0] == ("start", "***")
    assert spans[-1] == ("output", "***")


def test_call_llm_with_retry_records_aef_llm_span(monkeypatch, tmp_path):
    from new_core.llm.types import LLMMessage
    from ouroboros.agent import loop_llm_call as mod

    spans: list[tuple[str, Any]] = []

    class FakeLLM:
        def chat(self, **kwargs):
            return (
                {"role": "assistant", "content": "raw answer"},
                {
                    "provider": "openai",
                    "resolved_model": "openai/gpt-5.5",
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "cached_tokens": 4,
                },
            )

    class FakeLLMSpan:
        def add_giga_llm_result(self, *, response_headers, response_body):
            spans.append(("result", {"headers": response_headers, "body": response_body}))

    @contextmanager
    def fake_llm_request(*, request_headers, request_body):
        spans.append(("start", {"headers": request_headers, "body": request_body}))
        yield FakeLLMSpan()

    monkeypatch.setattr(mod.aef_tracing, "llm_request", fake_llm_request)

    usage: dict[str, Any] = {}
    msg = mod.call_llm_with_retry(
        FakeLLM(),
        [{"role": "user", "content": "raw prompt"}],
        "openai::gpt-5.5",
        [
            ToolSchema(
                name="demo_tool",
                description="raw description",
                parameters={"secret": "schema"},
            )
        ],
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

    assert msg == LLMMessage(content="raw answer", role="assistant")
    assert spans[0][0] == "start"
    request_body = spans[0][1]["body"]
    assert request_body["model"] == "openai::gpt-5.5"
    assert request_body["messages"] == [{"role": "user", "content": "***"}]
    assert "tools" not in request_body
    assert request_body["functions"][0] == {
        "name": "demo_tool",
        "description": "***",
        "parameters": {},
    }
    assert "raw prompt" not in json.dumps(request_body, ensure_ascii=False)
    assert "raw description" not in json.dumps(request_body, ensure_ascii=False)
    assert spans[-1][0] == "result"
    response_body = spans[-1][1]["body"]
    assert response_body["model"] == "openai/gpt-5.5"
    assert isinstance(response_body["created"], int)
    assert response_body["object"] == "chat.completion"
    assert response_body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "***",
    }
    assert response_body["usage"] == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
        "precached_prompt_tokens": 4,
    }
    assert "raw answer" not in json.dumps(response_body, ensure_ascii=False)


def test_aef_llm_masking_helpers_are_json_safe_and_do_not_leak_payload():
    from ouroboros.observability import aef_tracing as mod

    request_body = mod.mask_llm_request_payload(
        {
            "model": "model-a",
            "messages": [
                {
                    "role": "user",
                    "content": b"raw prompt",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "demo_tool", "arguments": b'{"secret": true}'},
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "demo_tool",
                        "description": b"raw description",
                        "parameters": {"secret": b"schema"},
                    },
                }
            ],
            "max_tokens": 10,
            "function_call": {"name": "demo_tool", "arguments": b"secret args"},
            "binary": b"secret",
        }
    )
    response_body = mod.mask_llm_response_payload(
        "model-a",
        {
            "role": "assistant",
            "content": b"raw answer",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "demo_tool", "arguments": b"secret args"},
                }
            ],
        },
        {
            "resolved_model": "model-b",
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "binary": b"secret usage",
        },
    )

    dumped = json.dumps(
        {"request": request_body, "response": response_body},
        ensure_ascii=False,
    )
    assert "raw prompt" not in dumped
    assert "raw answer" not in dumped
    assert "raw description" not in dumped
    assert "secret args" not in dumped
    assert request_body["messages"][0]["content"] == "***"
    assert request_body["function_call"] == {}
    assert "tool_calls" not in request_body["messages"][0]
    assert response_body["choices"][0]["message"]["function_call"]["arguments"] == {}
    assert response_body["usage"]["total_tokens"] == 12


def test_mask_llm_response_preserves_extended_b2bank_finish_reason():
    from ouroboros.observability import aef_tracing as mod

    response = mod.mask_llm_response_payload(
        "GigaChat-2-Max",
        {"role": "assistant", "content": "filtered", "finish_reason": "request_filter"},
        {},
    )

    assert response["choices"][0]["finish_reason"] == "request_filter"


def test_execute_single_tool_records_aef_tool_span(monkeypatch, tmp_path):

    spans: list[tuple[str, Any]] = []

    # Subclasses the interface rather than duck-typing it: the catalog reads
    # ``default_runtime`` to decide where the body runs, and the interface is what supplies it.
    class FakeProvider(IToolProvider):
        default_runtime = THREAD_RUNTIME

        def refresh(self):
            return

        def descriptors(self):
            return [self.get("demo_tool")]

        def get(self, name):
            from new_core.tools.interfaces import ToolDescriptor
            from new_core.tools.schema import ToolSchema

            if name != "demo_tool":
                return None
            return ToolDescriptor(
                schema=ToolSchema(name="demo_tool", description="", parameters={}),
                timeout_sec=360,
                origin="builtin",
                surface="discoverable",
                capability_class="read_only",
                safety_policy="check",
            )

        def refuse_dispatch(self, name):
            return None

        def execute(self, ctx, name, args):
            assert name == "demo_tool"
            assert args == {"value": 3}
            return "tool result"

    class FakeToolSpan:
        def add_tool_result(self, *, output):
            spans.append(("result", output))

    @contextmanager
    def fake_tool_run(*, input):
        spans.append(("start", input))
        yield FakeToolSpan()

    from ouroboros.observability import aef_tracing

    monkeypatch.setattr(aef_tracing, "tool_run", fake_tool_run)
    # The safety supervisor is a dispatch gate now, so it runs on this path too.
    monkeypatch.setattr("ouroboros.policy.safety.check_safety", lambda *a, **kw: (True, ""))

    from new_core.tools.context import ToolContext
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from tests._shared import collecting_event_sink

    executor = build_tool_call_executor(
        catalog=make_catalog([FakeProvider()]), drive_logs=tmp_path / "logs", events=collecting_event_sink()
    )
    result = asyncio.run(
        executor._execute_single_tool(
            ToolContext(repo_dir=tmp_path, drive_root=tmp_path),
            ToolCall(
                id="call-1",
                function=ToolCallFunction(name="demo_tool", arguments=json.dumps({"value": 3})),
            ),
            "task-1",
        )
    )

    assert result["result"] == "tool result"
    assert spans == [
        ("start", "***"),
        ("result", "***"),
    ]


def test_gather_gives_each_concurrent_call_its_own_task_and_context(tmp_path, monkeypatch):
    """8.3 — what keeps two overlapping tool spans from writing into each other.

    Tracing holds the session and trace ids in ``ContextVar``s, so span separation across a
    parallel batch rests entirely on each call getting its own copy of the context. Threads
    gave that for free; ``asyncio.gather`` gives it because it wraps each coroutine in a
    task, and a task copies the context at creation. Verified rather than assumed — the
    failure mode is two calls quietly sharing a trace id.
    """
    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.context import ToolContext
    from ouroboros.agent import loop_tool_execution as mod
    from ouroboros.observability import aef_tracing
    from ouroboros.policy.capabilities import READ_ONLY_PARALLEL_TOOLS
    from tests._shared import collecting_event_sink

    names = ["calculator", "data_read"]
    assert set(names) <= READ_ONLY_PARALLEL_TOOLS, "this must be the gather path, not the sequential one"

    probe = aef_tracing._CURRENT_TRACE_ID
    in_flight: list[str] = []
    seen: dict[str, tuple[str, int]] = {}

    async def _fake_single(self, ctx, tc, task_id="", redact_keys=frozenset()):
        name = str(tc.function.name)
        probe.set(f"trace-for-{name}")
        in_flight.append(name)
        while len(in_flight) < len(names):  # noqa: ASYNC110 - a barrier over two tasks, not a single event
            await asyncio.sleep(0.01)
        seen[name] = (probe.get(), id(asyncio.current_task()))
        return {
            "tool_call_id": tc.id,
            "fn_name": name,
            "result": f"{name}-done",
            "is_error": False,
            "tool_args": {},
            "args_for_log": {},
            "is_code_tool": False,
            "result_meta": {"status": "ok"},
        }

    monkeypatch.setattr(mod.ToolCallExecutor, "_execute_single_tool", _fake_single)
    caller_trace_id = probe.get()

    from ouroboros.agent.loop_adapters import build_tool_call_executor

    executor = build_tool_call_executor(
        catalog=make_catalog([]), drive_logs=tmp_path / "logs", events=collecting_event_sink()
    )
    results = executor.execute(
        [
            ToolCall(id=f"call-{i}", function=ToolCallFunction(name=name, arguments="{}"))
            for i, name in enumerate(names)
        ],
        ctx=ToolContext(repo_dir=tmp_path, drive_root=tmp_path),
        task_id="task-spans",
    )

    assert [r["result"] for r in results] == [f"{name}-done" for name in names]
    # Each call read back its own value while the other was mid-flight.
    assert {name: seen[name][0] for name in names} == {name: f"trace-for-{name}" for name in names}
    assert len({seen[name][1] for name in names}) == len(names), "the calls shared a task"
    assert probe.get() == caller_trace_id, "a call's context leaked back into the caller's"


def test_a2a_executor_records_input_request_span(monkeypatch):
    from ouroboros.ingress import a2a_executor as mod
    from tests.test_a2a_protocol import TestA2AExecutor, task_state_name

    spans: list[tuple[str, Any]] = []

    class FakeInputSpan:
        def add_response(self, *, headers, body, http_code=200):
            spans.append(("response", {"headers": headers, "body": body, "http_code": http_code}))

    @contextmanager
    def fake_input_request(*, headers, body, path, method, span_name="input_request"):
        spans.append(
            (
                "start",
                {
                    "headers": headers,
                    "body": body,
                    "path": path,
                    "method": method,
                    "span_name": span_name,
                },
            )
        )
        yield FakeInputSpan()

    async def fake_dispatch(self, text, eq, tid, cid, mid=""):
        return "mocked response"

    helper = TestA2AExecutor()
    executor = mod.OuroborosA2AExecutor(max_concurrent=3)
    monkeypatch.setattr(mod.aef_tracing, "input_request", fake_input_request)
    monkeypatch.setattr(mod.OuroborosA2AExecutor, "_dispatch_to_supervisor", fake_dispatch)

    ctx = helper._make_context(text="test question")
    eq, events = helper._make_event_queue()
    asyncio.run(executor.execute(ctx, eq))

    states = [task_state_name(e.status.state) for e in events if hasattr(e, "status")]
    assert "completed" in states
    trace_id = spans[0][1]["headers"]["X-Trace-Id"]
    assert len(trace_id) == 32
    assert spans[0] == (
        "start",
        {
            "headers": {"X-Trace-Id": trace_id},
            "body": "***",
            "path": "/a2a",
            "method": "POST",
            "span_name": "input_request",
        },
    )


def test_dialog_inbound_records_masked_input_request_and_session(monkeypatch, tmp_path):
    import supervisor.message_bus as message_bus
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from supervisor import server_runtime as mod
    from supervisor.messaging.base import InboundMessage
    from supervisor.orchestrator.intent import TaskIntentKind

    spans: list[tuple[str, Any]] = []
    sessions: list[str] = []

    class FakeInputSpan:
        def add_response(self, *, headers, body, http_code=200):
            spans.append(("response", {"headers": headers, "body": body, "http_code": http_code}))

    @contextmanager
    def fake_input_request(*, headers, body, path, method, span_name="input_request"):
        spans.append(
            (
                "start",
                {
                    "headers": headers,
                    "body": body,
                    "path": path,
                    "method": method,
                    "span_name": span_name,
                },
            )
        )
        yield FakeInputSpan()

    class FakeOrchestrator:
        def __init__(self):
            self.envelopes: list[Any] = []

        def submit(self, envelope):
            self.envelopes.append(envelope)
            return types.SimpleNamespace(intent=TaskIntentKind.AGENT_CHAT, slash_command="")

    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(message_bus, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.aef_tracing, "input_request", fake_input_request)
    monkeypatch.setattr(mod.aef_tracing, "set_session_id", lambda session_id: sessions.append(session_id))

    handled = mod._dispatch_runtime_inbound(
        message=InboundMessage(chat_id=42, user_id=99, text="secret prompt"),
        text="secret prompt",
        ctx=types.SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            load_state=lambda: OwnerRuntimeState(session_id="sess-in", owner_id=99),
            orchestrator=orchestrator,
            send_with_budget=lambda *args, **kwargs: None,
        ),
    )

    assert handled is True
    assert orchestrator.envelopes
    assert sessions == ["sess-in"]
    trace_id = spans[0][1]["headers"]["X-Trace-Id"]
    assert len(trace_id) == 32
    assert spans[0] == (
        "start",
        {
            "headers": {"X-Trace-Id": trace_id},
            "body": "***",
            "path": "/dialogs",
            "method": "POST",
            "span_name": "input_request",
        },
    )
    assert spans[-1] == (
        "response",
        {
            "headers": {"X-Trace-Id": trace_id},
            "body": "***",
            "http_code": 202,
        },
    )
    assert "secret prompt" not in repr(spans)


def test_dialog_outbound_records_masked_output_request_and_session(monkeypatch):
    import supervisor.message_bus as mod

    spans: list[tuple[str, Any]] = []
    sessions: list[str] = []
    sent: list[tuple[Any, ...]] = []

    class FakeOutputSpan:
        def add_response(self, *, headers, body, http_code=200):
            spans.append(("response", {"headers": headers, "body": body, "http_code": http_code}))

    @contextmanager
    def fake_output_request(*, headers, body, path, method, span_name="output_request"):
        spans.append(
            (
                "start",
                {
                    "headers": headers,
                    "body": body,
                    "path": path,
                    "method": method,
                    "span_name": span_name,
                },
            )
        )
        yield FakeOutputSpan()

    monkeypatch.setattr(mod, "load_state", lambda: types.SimpleNamespace(owner_id=7, session_id="sess-out"))
    monkeypatch.setattr(mod, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mod,
        "get_bridge",
        lambda: types.SimpleNamespace(send_message=lambda *args, **kwargs: sent.append((args, kwargs)) or (True, "ok")),
    )
    monkeypatch.setattr(mod.aef_tracing, "output_request", fake_output_request)
    monkeypatch.setattr(mod.aef_tracing, "set_session_id", lambda session_id: sessions.append(session_id))

    trace_id = "463ac35c9f6413ad48485a3953bb6124"
    mod.send_with_budget(42, "secret answer", task_id="task-1", trace_id=trace_id)

    assert sent
    assert sessions == ["sess-out"]
    assert spans[0] == (
        "start",
        {
            "headers": {"X-Trace-Id": trace_id},
            "body": "***",
            "path": "/dialogs/sendMessage",
            "method": "POST",
            "span_name": "output_request",
        },
    )
    assert spans[-1] == (
        "response",
        {
            "headers": {"X-Trace-Id": trace_id},
            "body": "***",
            "http_code": 200,
        },
    )
    assert "secret answer" not in repr(spans)


def test_dialog_outbound_does_not_trace_progress(monkeypatch):
    import supervisor.message_bus as mod

    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(mod, "load_state", lambda: types.SimpleNamespace(owner_id=7, session_id="sess-out"))
    monkeypatch.setattr(mod, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mod,
        "get_bridge",
        lambda: types.SimpleNamespace(send_message=lambda *args, **kwargs: sent.append((args, kwargs)) or (True, "ok")),
    )
    monkeypatch.setattr(
        mod.aef_tracing,
        "output_request",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("progress must not be traced")),
    )

    mod.send_with_budget(42, "progress text", is_progress=True, task_id="task-1")

    assert sent


def test_trace_id_helpers_generate_validate_and_preserve_header(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    mod.reset_aef_tracing_for_tests()
    generated = mod.ensure_trace_id("")

    assert len(generated) == 32
    assert generated == generated.lower()
    int(generated, 16)
    assert mod.ensure_trace_id("not-a-trace") != "not-a-trace"
    assert mod.ensure_trace_id("463ac35c9f6413ad48485a3953bb6124") == "463ac35c9f6413ad48485a3953bb6124"
    assert mod.mask_trace_headers(
        {
            "Authorization": "Bearer secret",
            "X-Trace-Id": "463ac35c9f6413ad48485a3953bb6124",
        }
    ) == {
        "Authorization": "***",
        "X-Trace-Id": "463ac35c9f6413ad48485a3953bb6124",
    }


def test_set_trace_id_updates_aef_framework_contextvar(monkeypatch):
    from ouroboros.observability import aef_tracing as mod

    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_FRAMEWORK_TRACE_CONTEXT",
        types.SimpleNamespace(set=lambda value: calls.append(value)),
    )

    mod.reset_aef_tracing_for_tests()
    token = mod.set_trace_id("463ac35c9f6413ad48485a3953bb6124")
    try:
        assert mod.current_trace_id() == "463ac35c9f6413ad48485a3953bb6124"
        assert calls == ["463ac35c9f6413ad48485a3953bb6124"]
    finally:
        mod.reset_trace_id(token)

    assert mod.current_trace_id() == ""


def test_mask_span_kwargs_blanket_masks_input_by_default():
    from ouroboros.observability.aef_tracing import _mask_span_kwargs

    masked = _mask_span_kwargs({"input": {"tool": "t", "arguments": {"q": "x"}}})
    assert masked["input"] == "***"


def test_mask_span_kwargs_premasked_input_reaches_sdk_json_safe():
    # The SDK-boundary contract for the tool-trace payload toggle: a premasked
    # input must survive to the factory (JSON-safe), not be re-masked to "***".
    from ouroboros.observability.aef_tracing import _mask_span_kwargs

    payload = {"tool": "t", "arguments": {"q": "x"}, "task_id": "id"}
    masked = _mask_span_kwargs({"input": payload}, premasked=("input",))
    assert masked["input"] == payload

    # Other span kinds keep the blanket mask untouched.
    assert _mask_span_kwargs({"body": payload})["body"] == "***"


def test_tool_run_declares_its_input_premasked(monkeypatch):
    import ouroboros.observability.aef_tracing as aef

    captured = {}

    def _fake_span_context(factory_name, *, premasked=(), **kwargs):
        captured["factory"] = factory_name
        captured["premasked"] = premasked
        captured["kwargs"] = kwargs
        from contextlib import nullcontext

        return nullcontext()

    monkeypatch.setattr(aef, "_span_context", _fake_span_context)
    with aef.tool_run(input={"tool": "t"}):
        pass

    assert captured["factory"] == "aef_tool_run"
    assert captured["premasked"] == ("input",)
    assert captured["kwargs"] == {"input": {"tool": "t"}}
