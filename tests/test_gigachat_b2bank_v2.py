from __future__ import annotations

import re

import pytest

from ouroboros.llm.providers.gigachat_b2bank import (
    B2BankProtocolError,
    _chat_url,
    _files_url,
    build_payload_v1,
    build_payload_v2,
    build_tool_name_map,
    normalize_response_v1,
    normalize_response_v2,
    parameters_schema,
)


def _target(version: str = "v1") -> dict:
    return {
        "resolved_model": "GigaChat-2-Max",
        "base_url": "https://b2bank.test/v2",
        "chat_api_version": version,
    }


def _tool(name: str = "namespace.tool2") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Look up a value",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }


def test_b2bank_urls_use_selected_chat_version_but_files_are_always_v1():
    assert _chat_url("https://b2bank.test", "v1") == "https://b2bank.test/v1/chat/completions"
    assert _chat_url("https://b2bank.test/v1", "v2") == "https://b2bank.test/v2/chat/completions"
    assert _chat_url("https://b2bank.test/v2", "v1") == "https://b2bank.test/v1/chat/completions"
    assert _files_url("https://b2bank.test/v2") == "https://b2bank.test/v1/files"


def test_tool_name_map_keeps_safe_names_and_hashes_unsafe_names_stably():
    long_name = "a" * 65
    tools = [
        _tool("lookup_weather"),
        _tool("namespace.tool2"),
        _tool("поиск.2"),
        _tool(long_name),
    ]

    first = build_tool_name_map(tools, [])
    second = build_tool_name_map(tools, [])

    assert first.runtime_to_provider["lookup_weather"] == "lookup_weather"
    for runtime_name in ("namespace.tool2", "поиск.2", long_name):
        alias = first.runtime_to_provider[runtime_name]
        assert re.fullmatch(r"f[a-z]{10}", alias)
        assert second.runtime_to_provider[runtime_name] == alias
        assert first.provider_to_runtime[alias] == runtime_name


def test_tool_name_map_extends_digest_to_resolve_collision(monkeypatch):
    monkeypatch.setattr(
        "ouroboros.llm.providers.gigachat_b2bank._tool_name_digest",
        lambda name: "abcdefghij" + ("kl" if name.endswith("1") else "mn"),
    )

    names = build_tool_name_map([_tool("bad.1"), _tool("bad.2")], [])

    assert names.runtime_to_provider["bad.1"] == "fabcdefghij"
    assert names.runtime_to_provider["bad.2"] == "fabcdefghijmn"


def test_v1_payload_merges_system_and_preserves_contract():
    tools = [_tool()]
    names = build_tool_name_map(tools, [])
    alias = names.runtime_to_provider["namespace.tool2"]

    payload = build_payload_v1(
        target=_target("v1"),
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
        ],
        max_tokens=128,
        tool_choice={"type": "function", "function": {"name": "namespace.tool2"}},
        temperature=0.2,
        tools=tools,
        tool_names=names,
    )

    assert payload["messages"] == [
        {"role": "system", "content": "first\n\nsecond"},
        {"role": "user", "content": "hello"},
    ]
    assert payload["functions"][0]["name"] == alias
    assert payload["function_call"] == {"name": alias}
    assert "reasoning_effort" not in payload


def test_v1_payload_passes_reasoning_effort_at_root():
    payload = build_payload_v1(
        target=_target("v1"),
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="low",
    )

    assert payload["reasoning_effort"] == "low"


@pytest.mark.parametrize(("internal", "wire"), [("xhigh", "high"), ("minimal", "low"), ("turbo", "medium")])
def test_internal_effort_levels_clamp_to_wire_set(internal, wire):
    # The internal effort set is wider than the wire set; the wire must only
    # ever see the levels the provider APIs document.
    v1 = build_payload_v1(
        target=_target("v1"),
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort=internal,
    )
    assert v1["reasoning_effort"] == wire

    v2 = build_payload_v2(
        target=_target("v2"),
        messages=[{"role": "user", "content": "hello"}],
        reasoning_effort=internal,
        max_tokens=64,
        tool_choice="none",
        temperature=None,
        tools=None,
    )
    assert v2["model_options"]["reasoning"] == {"effort": wire}


def test_off_is_a_disable_synonym_and_stays_off_the_wire():
    # Disabling is not unified across models yet ("off" vs "none"); both words
    # must keep reasoning off the wire instead of clamping to an enable level.
    v1 = build_payload_v1(
        target=_target("v1"),
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="off",
    )
    assert "reasoning_effort" not in v1

    v2 = build_payload_v2(
        target=_target("v2"),
        messages=[{"role": "user", "content": "hello"}],
        reasoning_effort="off",
        max_tokens=64,
        tool_choice="none",
        temperature=None,
        tools=None,
    )
    assert "reasoning" not in v2["model_options"]


def test_v1_response_extracts_reasoning_content():
    message, _ = normalize_response_v1(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "chain of thought",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        },
        _target("v1"),
        request_id="req",
    )

    assert message["content"] == "answer"
    assert message["reasoning_content"] == "chain of thought"


def test_v2_payload_uses_blocks_tool_state_and_reasoning_mapping():
    tools = [_tool()]
    messages = [
        {"role": "system", "content": "one"},
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "two"},
        {
            "role": "assistant",
            "content": "calling",
            "reasoning_content": "thinking",
            "tools_state_id": "state-1",
            "tool_calls": [
                {
                    "id": "old-call",
                    "type": "function",
                    "function": {"name": "namespace.tool2", "arguments": '{"query":"x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": '{"value":1}'},
    ]
    names = build_tool_name_map(tools, messages)
    alias = names.runtime_to_provider["namespace.tool2"]

    payload = build_payload_v2(
        target=_target("v2"),
        messages=messages,
        reasoning_effort="high",
        max_tokens=256,
        tool_choice={"type": "function", "function": {"name": "namespace.tool2"}},
        temperature=0.1,
        tools=tools,
        tool_names=names,
    )

    assert payload["messages"] == [
        {"role": "system", "content": [{"text": "one\n\ntwo"}]},
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "reasoning", "content": [{"text": "thinking"}]},
        {
            "role": "assistant",
            "content": [
                {"text": "calling"},
                {"function_call": {"name": alias, "arguments": {"query": "x"}}},
            ],
            "tools_state_id": "state-1",
        },
        {
            "role": "tool",
            "content": [{"function_result": {"name": alias, "result": '{"value":1}'}}],
        },
    ]
    assert payload["model_options"] == {
        "max_tokens": 256,
        "temperature": 0.1,
        "reasoning": {"effort": "high"},
    }
    assert payload["tools"] == [
        {
            "functions": {
                "specifications": [
                    {
                        "name": alias,
                        "description": "Look up a value",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    }
                ]
            }
        }
    ]
    assert payload["tool_config"] == {"mode": "forced", "function_name": alias}


def test_v2_none_reasoning_and_tool_choice_are_omitted_or_disabled():
    payload = build_payload_v2(
        target=_target("v2"),
        messages=[{"role": "user", "content": "hello"}],
        reasoning_effort="none",
        max_tokens=64,
        tool_choice="none",
        temperature=None,
        tools=[_tool("safe_tool")],
    )

    assert "reasoning" not in payload["model_options"]
    assert payload["tool_config"] == {"mode": "none"}


def test_v2_parallel_tool_calls_follows_target_flag():
    enabled = build_payload_v2(
        target={**_target("v2"), "parallel_tool_calls": True},
        messages=[{"role": "user", "content": "hello"}],
        reasoning_effort="none",
        max_tokens=64,
        tool_choice="none",
        temperature=None,
        tools=None,
    )
    assert enabled["model_options"]["parallel_tool_calls"] is True

    # Default target (flag unset) must not put the key on the wire.
    default = build_payload_v2(
        target=_target("v2"),
        messages=[{"role": "user", "content": "hello"}],
        reasoning_effort="none",
        max_tokens=64,
        tool_choice="none",
        temperature=None,
        tools=None,
    )
    assert "parallel_tool_calls" not in default["model_options"]


def test_parallel_tool_calls_setting_coerces_env_strings() -> None:
    from ouroboros.storage.settings.schema import AppSettings

    # Env vars arrive as strings; the shared bool validator must pattern-match
    # them instead of pydantic-native coercion (which rejects "" outright).
    assert AppSettings(GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS="true").GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS is True
    assert AppSettings(GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS="false").GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS is False
    assert AppSettings(GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS="").GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS is False


def test_v1_history_with_multiple_tool_calls_fails_loud_and_named():
    # parallel_tool_calls is v2-only: if multi-call history ever replays through
    # the v1 builder (unsupported mid-flight version downgrade), the failure must
    # stay loud and name the exact cause instead of silently dropping calls.
    tools = [_tool("namespace.tool2"), _tool("other.3")]
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "namespace.tool2", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "other.3", "arguments": "{}"}},
            ],
        },
    ]
    with pytest.raises(B2BankProtocolError, match="cannot represent multiple assistant tool calls"):
        build_payload_v1(
            target=_target("v1"),
            messages=messages,
            max_tokens=64,
            tool_choice="auto",
            temperature=None,
            tools=tools,
        )


def test_resolve_target_projects_parallel_tool_calls_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.llm.models import parse_model_ref
    from ouroboros.llm.providers.gigachat_b2bank import GigachatB2BankProvider
    from ouroboros.storage.settings.schema import AppSettings

    for flag, expected in ((True, True), (False, False)):
        monkeypatch.setattr(
            "ouroboros.storage.settings.store.load_settings",
            lambda flag=flag: AppSettings(GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS=flag),
        )
        target = GigachatB2BankProvider().resolve_target(parse_model_ref("gigachat-b2bank::GigaChat-2-Max"))
        assert target["parallel_tool_calls"] is expected


def test_v2_response_collects_text_reasoning_multiple_calls_and_usage():
    tools = [_tool("namespace.tool2"), _tool("other.3")]
    names = build_tool_name_map(tools, [])
    first_alias = names.runtime_to_provider["namespace.tool2"]
    second_alias = names.runtime_to_provider["other.3"]

    message, usage = normalize_response_v2(
        {
            "messages": [
                {"role": "reasoning", "content": [{"text": "think"}]},
                {
                    "role": "assistant",
                    "tools_state_id": "state-2",
                    "content": [
                        {"text": "answer"},
                        {"function_call": {"name": first_alias, "arguments": {"query": "a"}}},
                        {"function_call": {"name": second_alias, "arguments": {"query": "b"}}},
                    ],
                },
            ],
            "finish_reason": "function_call",
            "usage": {
                "input_tokens": 11,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens": 7,
                "total_tokens": 18,
            },
        },
        _target("v2"),
        tool_names=names,
        request_id="req42",
    )

    assert message["content"] == "answer"
    assert message["reasoning_content"] == "think"
    assert message["finish_reason"] == "function_call"
    assert message["tools_state_id"] == "state-2"
    assert [call["id"] for call in message["tool_calls"]] == [
        "call_gb_req42_0",
        "call_gb_req42_1",
    ]
    assert [call["function"]["name"] for call in message["tool_calls"]] == [
        "namespace.tool2",
        "other.3",
    ]
    assert usage["cached_tokens"] == 4
    assert usage["total_tokens"] == 18


def test_v2_response_accepts_actual_singular_tool_state_id():
    message, _usage = normalize_response_v2(
        {
            "messages": [
                {
                    "role": "assistant",
                    "tool_state_id": "state-from-provider",
                    "content": [
                        {
                            "function_call": {"name": "safe_tool", "arguments": {"query": "x"}},
                        }
                    ],
                }
            ],
            "finish_reason": "function_call",
            "usage": {
                "input_tokens": 1,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 1,
                "total_tokens": 2,
            },
        },
        _target("v2"),
        tool_names=build_tool_name_map([_tool("safe_tool")], []),
        request_id="req-singular-state",
    )

    assert message["tools_state_id"] == "state-from-provider"


@pytest.mark.parametrize(
    "response",
    [
        {"messages": [], "finish_reason": "stop", "usage": {}},
        {"messages": [{"role": "assistant", "content": "wrong"}], "finish_reason": "stop"},
        {
            "messages": [{"role": "assistant", "content": []}],
            "finish_reason": "function_call",
            "usage": {},
        },
        {
            "messages": [{"role": "assistant", "content": []}],
            "finish_reason": "function_call_error",
            "usage": {},
        },
    ],
)
def test_v2_rejects_structurally_invalid_success_responses(response):
    with pytest.raises(B2BankProtocolError):
        normalize_response_v2(response, _target("v2"), request_id="req")


@pytest.mark.parametrize(
    "finish_reason",
    [
        "stop",
        "length",
        "blacklist",
        "request_blacklist",
        "request_whitelist",
        "request_filter",
        "response_blacklist",
    ],
)
def test_v2_preserves_every_non_error_finish_reason(finish_reason):
    message, _ = normalize_response_v2(
        {
            "messages": [{"role": "assistant", "content": [{"text": "answer"}]}],
            "finish_reason": finish_reason,
            "usage": {},
        },
        _target("v2"),
        request_id="req",
    )

    assert message["finish_reason"] == finish_reason


def test_v2_rejects_function_call_in_reasoning_message():
    with pytest.raises(B2BankProtocolError, match="assistant message"):
        normalize_response_v2(
            {
                "messages": [
                    {
                        "role": "reasoning",
                        "content": [{"function_call": {"name": "safe_tool", "arguments": {}}}],
                    },
                    {"role": "assistant", "content": [{"text": "answer"}]},
                ],
                "finish_reason": "stop",
                "usage": {},
            },
            _target("v2"),
            request_id="req",
        )


def test_v1_rejects_unknown_alias_and_preserves_finish_reason():
    tools = [_tool("namespace.tool2")]
    names = build_tool_name_map(tools, [])
    valid_alias = names.runtime_to_provider["namespace.tool2"]
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "function_call": {"name": valid_alias, "arguments": {}},
                },
                "finish_reason": "function_call",
            }
        ],
        "usage": {},
    }

    message, _ = normalize_response_v1(
        response,
        _target("v1"),
        tool_names=names,
        request_id="req1",
    )
    assert message["finish_reason"] == "function_call"
    assert message["tool_calls"][0]["id"] == "call_gb_req1_0"

    response["choices"][0]["message"]["function_call"]["name"] = "funknownaa"
    with pytest.raises(B2BankProtocolError, match="unknown function alias"):
        normalize_response_v1(response, _target("v1"), tool_names=names, request_id="req1")


@pytest.mark.parametrize("finish_reason", ["stop", "length", "blacklist", "error"])
def test_v1_preserves_every_non_function_finish_reason(finish_reason):
    message, _ = normalize_response_v1(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "answer"},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {},
        },
        _target("v1"),
        request_id="req",
    )

    assert message["finish_reason"] == finish_reason


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "function_call",
                }
            ],
        },
    ],
)
def test_v1_rejects_structurally_invalid_success_responses(response):
    with pytest.raises(B2BankProtocolError):
        normalize_response_v1(response, _target("v1"), request_id="req")


def test_schema_normalization_handles_objects_combinators_and_refs():
    schema = parameters_schema(
        {
            "properties": {
                "mapping": {"additionalProperties": {"type": "string"}},
                "items": {"type": "array", "items": {"type": "object"}},
                "choice": {
                    "anyOf": [
                        {"properties": {"nested": {"type": "object"}}},
                        {"$ref": "#/$defs/value"},
                    ],
                },
            },
            "allOf": [{"type": "object"}],
        }
    )

    assert schema["type"] == "object"
    assert schema["properties"]["mapping"]["type"] == "object"
    assert schema["properties"]["mapping"]["properties"] == {}
    assert schema["properties"]["items"]["items"]["properties"] == {}
    assert schema["properties"]["choice"]["anyOf"][0]["type"] == "object"
    assert schema["properties"]["choice"]["anyOf"][0]["properties"]["nested"]["properties"] == {}
    assert schema["properties"]["choice"]["anyOf"][1] == {"$ref": "#/$defs/value"}
    assert schema["allOf"][0]["properties"] == {}


@pytest.mark.parametrize(
    "schema",
    [
        {"allOf": [{"type": "object"}]},
        {"oneOf": [{"type": "object"}]},
        {"anyOf": [{"type": "object"}]},
        {"$ref": "#/$defs/arguments"},
    ],
)
def test_schema_normalization_always_adds_root_properties_without_retyping(schema):
    normalized = parameters_schema(schema)

    assert normalized["properties"] == {}
    assert "type" not in normalized
