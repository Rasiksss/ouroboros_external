"""Подставной сервер модели: формат OpenAI, ошибки, журнал, смена сценария (шаг 07)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from stub_model.client import StubModelAdmin
from stub_model.scenario import parse_scenario
from stub_model.server import StubModelServer

HELLO = {
    "name": "hello",
    "rules": [
        {
            "id": "hello",
            "when": {"user_contains": "привет"},
            "reply": {"text": "Здравствуйте!"},
        },
        {
            "id": "ask-tool",
            "when": {"user_contains": "посчитай"},
            "reply": {"tool_calls": [{"name": "code_exec", "arguments": {"code": "print(2+2)"}}]},
        },
        {
            "id": "boom",
            "when": {"user_contains": "сломайся"},
            "reply": {"error": {"status": 503, "message": "занято"}},
        },
    ],
}


@pytest.fixture
def server() -> Iterator[StubModelServer]:
    stub = StubModelServer.start("127.0.0.1", 0, parse_scenario(HELLO))
    try:
        yield stub
    finally:
        stub.stop()


def _post(stub: StubModelServer, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{stub.url}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _ask(text: str, model: str = "stub-main", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": text}], **extra}


def test_text_rule_answers_in_the_openai_chat_completion_shape(server: StubModelServer) -> None:
    status, body = _post(server, _ask("привет"))
    choice = body["choices"][0]
    assert status == 200
    assert (body["object"], body["model"]) == ("chat.completion", "stub-main")
    assert choice["message"] == {"role": "assistant", "content": "Здравствуйте!"}
    assert choice["finish_reason"] == "stop"
    assert (
        body["usage"]["total_tokens"]
        == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


def test_tool_call_rule_answers_with_json_string_arguments(server: StubModelServer) -> None:
    """Вызов инструмента в формате OpenAI: `arguments` — строка JSON, причина — tool_calls."""
    _, body = _post(server, _ask("посчитай"))
    choice = body["choices"][0]
    call = choice["message"]["tool_calls"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert call["type"] == "function" and call["id"]
    assert call["function"]["name"] == "code_exec"
    assert json.loads(call["function"]["arguments"]) == {"code": "print(2+2)"}


def test_error_rule_answers_with_its_status_and_message(server: StubModelServer) -> None:
    status, body = _post(server, _ask("сломайся"))
    assert status == 503
    assert body["error"]["message"] == "занято"


def test_request_no_rule_matches_fails_loudly_instead_of_hanging(server: StubModelServer) -> None:
    """Нет совпадения — 400 с текстом, что спросили и какие правила есть; тест падает, не висит."""
    status, body = _post(server, _ask("а это что", model="stub-light"))
    assert status == 400
    message = body["error"]["message"]
    assert "stub-model" in message and "stub-light" in message and "а это что" in message
    assert "hello" in message and "ask-tool" in message


def test_streaming_is_refused_with_a_clear_text(server: StubModelServer) -> None:
    """Клиент ouroboros не потоковый (openai_compat.py); поток — значит, что-то изменилось."""
    status, body = _post(server, _ask("привет", stream=True))
    assert status == 400
    assert "stream" in body["error"]["message"]


def test_same_request_gets_the_same_answer_twice(server: StubModelServer) -> None:
    first = _post(server, _ask("привет"))[1]["choices"][0]["message"]
    second = _post(server, _ask("привет"))[1]["choices"][0]["message"]
    assert first == second


def test_journal_keeps_what_the_model_was_shown_and_which_rule_answered(
    server: StubModelServer,
) -> None:
    admin = StubModelAdmin(server.url)
    _post(server, _ask("привет", tools=[{"type": "function", "function": {"name": "code_exec"}}]))
    _post(server, _ask("не знаю такого"))
    first, second = admin.journal()
    assert (first["rule"], second["rule"]) == ("hello", None)
    assert first["tools"] == ["code_exec"] and first["user"] == "привет"
    assert admin.saw("привет") and not admin.saw("секретный документ")
    admin.clear()
    assert admin.journal() == []


def test_put_scenario_replaces_the_rules_and_a_bad_one_keeps_the_old(
    server: StubModelServer,
) -> None:
    admin = StubModelAdmin(server.url)
    admin.use_scenario(
        {"name": "new", "rules": [{"id": "n", "when": {}, "reply": {"text": "новое"}}]}
    )
    assert _post(server, _ask("привет"))[1]["choices"][0]["message"]["content"] == "новое"
    with pytest.raises(RuntimeError, match="when"):
        admin.use_scenario({"name": "bad", "rules": [{"id": "b", "reply": {"text": "x"}}]})
    assert _post(server, _ask("привет"))[1]["choices"][0]["message"]["content"] == "новое"


def test_server_without_a_scenario_says_so(server: StubModelServer) -> None:
    empty = StubModelServer.start("127.0.0.1", 0, None)
    try:
        status, body = _post(empty, _ask("привет"))
    finally:
        empty.stop()
    assert status == 400 and "сценарий не загружен" in body["error"]["message"]
