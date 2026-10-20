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


# Так О дописывает итог круга с инструментом (`ouroboros/agent/loop_adapters.py`, журнал ревью 07).
LEDGER_NOTE = (
    "[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]\nTool outcome ledger:\n"
    "- t status=ok failure=ok args=44136fa355b3678a\n[/SYSTEM RUNTIME NOTE]"
)


def _system() -> dict[str, Any]:
    return {"role": "system", "content": [{"type": "text", "text": "правила; история чата"}]}


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
        _rule("hello", {"user_contains": "привет"}, {"text": "второе"}),
    )
    rule = scenario.pick(_view(_user("привет")))
    assert rule is not None and rule.id == "any"


def test_every_condition_of_a_rule_must_hold() -> None:
    """Условия правила соединены «и»: модель подошла, а круг нет — правило не сработало."""
    scenario = _scenario(_rule("r", {"model": "stub-light", "round": 2}, {"text": "x"}))
    assert scenario.pick(_view(_user("привет"), model="stub-light")) is None
    second_round = _view(_user("привет"), _assistant(calls=1), _tool("OK"), model="stub-light")
    assert scenario.pick(second_round) is not None


def test_notes_ouroboros_appends_after_a_tool_keep_the_round_going() -> None:
    """Б1 (ревью 07): записка О после инструмента — `user`, но не реплика: круг идёт дальше."""
    view = _view(_system(), _user("посчитай"), _assistant(calls=1), _tool("4"), _user(LEDGER_NOTE))
    assert (view.round, view.user, view.last_tool_result) == (2, "посчитай", "4")


def test_round_counts_every_model_answer_in_the_request() -> None:
    """Круг — 1 + число ответов модели в запросе; история чата у О — в системном сообщении."""
    assert _view(_system(), _user("а")).round == 1
    third = _view(
        _system(),
        _user("а"),
        _assistant(calls=1),
        _tool("OK"),
        _user(LEDGER_NOTE),
        _assistant(calls=2),
        _tool("OK first"),
        _tool("E42 second"),
        _user(LEDGER_NOTE),
    )
    assert (third.round, third.last_tool_result) == (3, "E42 second")
    scenario = _scenario(_rule("r", {"last_tool_result_starts_with": "E42"}, {"text": "x"}))
    assert scenario.pick(third) is not None
    assert scenario.pick(_view(_system(), _user("а"), _assistant(calls=1), _tool("OK"))) is None


def test_message_content_given_as_parts_is_read_as_text() -> None:
    """Содержимое сообщения бывает списком частей: текстовые части склеиваются."""
    parts = {
        "role": "user",
        "content": [{"type": "text", "text": "при"}, {"type": "text", "text": "вет"}],
    }
    assert _view(parts).user == "привет"


def test_rule_without_when_is_refused() -> None:
    """Правило без `when` не читается как «любой запрос»: универсальное правило пишут `{}`."""
    with pytest.raises(ScenarioError, match="bare.*when"):
        _scenario({"id": "bare", "reply": {"text": "x"}})


def test_unknown_key_is_refused_with_the_rule_id() -> None:
    """Опечатка в условии не должна молча превратить правило в универсальное."""
    with pytest.raises(ScenarioError, match="typo.*user_contain"):
        _scenario(_rule("typo", {"user_contain": "привет"}, {"text": "x"}))


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


def test_an_argument_can_be_taken_from_the_last_tool_result() -> None:
    """Шаг 16: корень узнаёт `task_id` подзадачи только из ответа `schedule_task` — аргумент берётся
    из результата."""
    scenario = parse_scenario(
        {
            "rules": [
                {
                    "id": "wait-for-the-child",
                    "when": {"last_tool_result_starts_with": "Task request queued"},
                    "reply": {
                        "tool_calls": [
                            {
                                "name": "aifa_wait",
                                "arguments": {"wait_sec": 30},
                                "arguments_from_last_tool_result": {
                                    "task_id": "queued ([0-9a-f]+):"
                                },
                            }
                        ]
                    },
                }
            ]
        }
    )
    view = request_view(
        _payload(_user("q"), {"role": "tool", "content": "Task request queued ab12cd34: Навык: x"})
    )

    rule = scenario.pick(view)
    assert rule is not None
    (call,) = rule.reply.tool_calls

    assert call.arguments_for(view) == {"wait_sec": 30, "task_id": "ab12cd34"}


def test_an_argument_that_the_result_does_not_give_is_left_empty_not_invented() -> None:
    scenario = parse_scenario(
        {
            "rules": [
                {
                    "id": "wait",
                    "when": {},
                    "reply": {
                        "tool_calls": [
                            {
                                "name": "aifa_wait",
                                "arguments_from_last_tool_result": {
                                    "task_id": "queued ([0-9a-f]+):"
                                },
                            }
                        ]
                    },
                }
            ]
        }
    )
    view = request_view(_payload(_user("q"), {"role": "tool", "content": "ничего похожего"}))

    rule = scenario.pick(view)
    assert rule is not None
    (call,) = rule.reply.tool_calls

    assert call.arguments_for(view) == {"task_id": ""}


def test_a_pattern_without_one_group_is_refused_when_the_scenario_loads() -> None:
    with pytest.raises(ScenarioError, match="одну группу"):
        parse_scenario(
            {
                "rules": [
                    {
                        "id": "bad",
                        "when": {},
                        "reply": {
                            "tool_calls": [
                                {"name": "t", "arguments_from_last_tool_result": {"a": "без групп"}}
                            ]
                        },
                    }
                ]
            }
        )
