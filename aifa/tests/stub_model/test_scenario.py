"""Сценарий подставной модели: правила, условия, отказ на неясное (ARCHITECTURE.md §2, шаг 07)."""

from __future__ import annotations

from typing import Any

import pytest

from stub_model.scenario import RequestView, ScenarioError, parse_scenario, request_view


def _payload(*messages: dict[str, Any], model: str = "stub-main", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": list(messages), **extra}


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str = "", calls: int = 0) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            for i in range(calls)
        ]
    return message


def _tool(text: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "c0", "content": text}


def _view(*messages: dict[str, Any], model: str = "stub-main") -> RequestView:
    return request_view(_payload(*messages, model=model))


def _scenario(*rules: dict[str, Any]) -> Any:
    return parse_scenario({"name": "t", "rules": list(rules)})


def _rule(rule_id: str, when: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
    return {"id": rule_id, "when": when, "reply": reply}


def test_first_matching_rule_wins() -> None:
    """Правила идут по порядку: подходит первое, а не самое точное."""
    scenario = _scenario(
        _rule("any", {}, {"text": "первое"}),
        _rule("hello", {"last_user_contains": "привет"}, {"text": "второе"}),
    )
    rule = scenario.pick(_view(_user("привет")))
    assert rule is not None and rule.id == "any"


def test_every_condition_of_a_rule_must_hold() -> None:
    """Условия правила соединены «и»: модель подошла, а круг нет — правило не сработало."""
    scenario = _scenario(_rule("r", {"model": "stub-light", "round": 2}, {"text": "x"}))
    assert scenario.pick(_view(_user("привет"), model="stub-light")) is None
    second_round = _view(_user("привет"), _assistant(calls=1), _tool("OK"), model="stub-light")
    assert scenario.pick(second_round) is not None


def test_round_counts_assistant_messages_after_the_last_user_message() -> None:
    """Круг — 1 + число ответов модели после последней реплики пользователя."""
    history = (_user("старое"), _assistant("ответ"), _user("новое"))
    assert _view(*history).round == 1
    assert (
        _view(*history, _assistant(calls=1), _tool("OK"), _assistant(calls=1), _tool("OK")).round
        == 3
    )


def test_last_tool_result_is_the_last_tool_message_after_the_last_user_message() -> None:
    """Результат инструмента прошлого хода не принимается за результат этого."""
    assert _view(_user("а"), _tool("OLD"), _user("б")).last_tool_result == ""
    after = _view(_user("а"), _assistant(calls=2), _tool("OK first"), _tool("E42 second"))
    assert after.last_tool_result == "E42 second"
    scenario = _scenario(_rule("r", {"last_tool_result_starts_with": "E42"}, {"text": "x"}))
    assert scenario.pick(after) is not None
    assert scenario.pick(_view(_user("а"), _assistant(calls=1), _tool("OK"))) is None


def test_message_content_given_as_parts_is_read_as_text() -> None:
    """Содержимое сообщения бывает списком частей: текстовые части склеиваются."""
    parts = {
        "role": "user",
        "content": [{"type": "text", "text": "при"}, {"type": "text", "text": "вет"}],
    }
    assert _view(parts).last_user == "привет"


def test_rule_without_when_is_refused() -> None:
    """Правило без `when` не читается как «любой запрос»: универсальное правило пишут `{}`."""
    with pytest.raises(ScenarioError, match="bare.*when"):
        _scenario({"id": "bare", "reply": {"text": "x"}})


def test_unknown_key_is_refused_with_the_rule_id() -> None:
    """Опечатка в условии не должна молча превратить правило в универсальное."""
    with pytest.raises(ScenarioError, match="typo.*last_user_contain"):
        _scenario(_rule("typo", {"last_user_contain": "привет"}, {"text": "x"}))


def test_reply_is_text_with_tool_calls_or_an_error_but_not_both() -> None:
    """Ответ: текст и/или вызовы инструментов, либо ошибка; смесь и пустота отклоняются."""
    with pytest.raises(ScenarioError, match="mixed"):
        _scenario(_rule("mixed", {}, {"text": "x", "error": {"status": 500, "message": "m"}}))
    with pytest.raises(ScenarioError, match="empty"):
        _scenario(_rule("empty", {}, {}))
    ok = _scenario(
        _rule("calls", {}, {"tool_calls": [{"name": "aifa_goal", "arguments": {"a": 1}}]})
    )
    assert ok.rules[0].reply.tool_calls[0].name == "aifa_goal"


def test_duplicate_rule_ids_are_refused() -> None:
    """По идентификатору правила журнал говорит, что сработало: он должен быть один."""
    with pytest.raises(ScenarioError, match="same"):
        _scenario(_rule("same", {}, {"text": "a"}), _rule("same", {}, {"text": "b"}))


def test_scenario_without_rules_is_refused() -> None:
    with pytest.raises(ScenarioError, match="rules"):
        parse_scenario({"name": "t", "rules": []})
