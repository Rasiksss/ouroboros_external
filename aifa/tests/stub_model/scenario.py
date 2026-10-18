"""Сценарий подставной модели: список правил «условие → ответ» (формат — README.md).

Только стандартная библиотека: модуль работает и в контейнере `python:3.12-slim`, и в тестах.
Сопоставление нарочно глупое: условия правила соединены «и», правила идут по порядку, срабатывает
первое. Ничего «умного» здесь быть не должно — иначе непонятно, почему модель ответила так.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WHEN_KEYS = frozenset({"model", "last_user_contains", "last_tool_result_starts_with", "round"})
REPLY_KEYS = frozenset({"text", "tool_calls", "error"})
RULE_KEYS = frozenset({"id", "when", "reply"})


class ScenarioError(ValueError):
    """Сценарий написан неверно; текст называет правило и поле."""


@dataclass(frozen=True)
class RequestView:
    """То, что правила видят в запросе: не весь запрос, а то, по чему они выбирают."""

    model: str
    stream: bool
    round: int
    last_user: str
    last_tool_result: str


def message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, Mapping))
    return ""


def request_view(payload: Mapping[str, Any]) -> RequestView:
    """Круг — 1 + число ответов модели после последней реплики пользователя; результат
    инструмента — последний `tool` после неё (результат прошлого хода не считается)."""
    messages = [m for m in payload.get("messages") or [] if isinstance(m, Mapping)]
    last_user_at = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    after = messages[last_user_at + 1 :]
    tool_results = [message_text(m) for m in after if m.get("role") == "tool"]
    return RequestView(
        model=str(payload.get("model", "")),
        stream=bool(payload.get("stream")),
        round=1 + sum(1 for m in after if m.get("role") == "assistant"),
        last_user=message_text(messages[last_user_at]) if last_user_at >= 0 else "",
        last_tool_result=tool_results[-1] if tool_results else "",
    )


@dataclass(frozen=True)
class When:
    model: str | None = None
    last_user_contains: str | None = None
    last_tool_result_starts_with: str | None = None
    round: int | None = None

    def matches(self, view: RequestView) -> bool:
        return (
            (self.model is None or view.model == self.model)
            and (self.last_user_contains is None or self.last_user_contains in view.last_user)
            and (
                self.last_tool_result_starts_with is None
                or view.last_tool_result.startswith(self.last_tool_result_starts_with)
            )
            and (self.round is None or view.round == self.round)
        )


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ReplyError:
    status: int
    message: str


@dataclass(frozen=True)
class Reply:
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    error: ReplyError | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    when: When
    reply: Reply


@dataclass(frozen=True)
class Scenario:
    name: str
    rules: tuple[Rule, ...]

    def pick(self, view: RequestView) -> Rule | None:
        return next((rule for rule in self.rules if rule.when.matches(view)), None)


def _unknown(rule_id: str, where: str, got: Mapping[str, Any], allowed: frozenset[str]) -> None:
    extra = sorted(set(got) - allowed)
    if extra:
        raise ScenarioError(
            f"правило {rule_id!r}: неизвестные поля в {where}: {extra}; допустимы {sorted(allowed)}"
        )


def _parse_when(rule_id: str, raw: Any) -> When:
    if not isinstance(raw, Mapping):
        raise ScenarioError(
            f'правило {rule_id!r}: нет `when` (универсальное правило — `"when": {{}}`)'
        )
    _unknown(rule_id, "when", raw, WHEN_KEYS)
    for key in ("model", "last_user_contains", "last_tool_result_starts_with"):
        if key in raw and not isinstance(raw[key], str):
            raise ScenarioError(f"правило {rule_id!r}: when.{key} должно быть строкой")
    if "round" in raw and (not isinstance(raw["round"], int) or raw["round"] < 1):
        raise ScenarioError(f"правило {rule_id!r}: when.round — целое от 1")
    return When(**dict(raw))


def _parse_tool_call(rule_id: str, raw: Any) -> ToolCall:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str):
        raise ScenarioError(f"правило {rule_id!r}: вызов инструмента — {{name, arguments}}")
    arguments = raw.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ScenarioError(f"правило {rule_id!r}: arguments вызова {raw['name']!r} — объект")
    return ToolCall(name=raw["name"], arguments=dict(arguments))


def _parse_reply(rule_id: str, raw: Any) -> Reply:
    if not isinstance(raw, Mapping):
        raise ScenarioError(f"правило {rule_id!r}: нет `reply`")
    _unknown(rule_id, "reply", raw, REPLY_KEYS)
    if not raw:
        raise ScenarioError(f"правило {rule_id!r}: пустой ответ — нужен text, tool_calls или error")
    if "error" in raw:
        if len(raw) > 1:
            raise ScenarioError(
                f"правило {rule_id!r}: ответ-ошибка нельзя смешивать с text/tool_calls"
            )
        error = raw["error"]
        if not isinstance(error, Mapping) or not isinstance(error.get("status"), int):
            raise ScenarioError(f"правило {rule_id!r}: error — {{status: число, message: текст}}")
        return Reply(error=ReplyError(error["status"], str(error.get("message", ""))))
    text = raw.get("text")
    if text is not None and not isinstance(text, str):
        raise ScenarioError(f"правило {rule_id!r}: text должен быть строкой")
    calls = raw.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ScenarioError(f"правило {rule_id!r}: tool_calls — список")
    return Reply(text=text, tool_calls=tuple(_parse_tool_call(rule_id, c) for c in calls))


def parse_scenario(data: Any) -> Scenario:
    if (
        not isinstance(data, Mapping)
        or not isinstance(data.get("rules"), list)
        or not data["rules"]
    ):
        raise ScenarioError(
            "сценарий — объект {name, rules: [...]} хотя бы с одним правилом (rules)"
        )
    rules: list[Rule] = []
    for raw in data["rules"]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise ScenarioError("каждое правило — объект с непустым `id`")
        rule_id = raw["id"]
        _unknown(rule_id, "правиле", raw, RULE_KEYS)
        if any(rule.id == rule_id for rule in rules):
            raise ScenarioError(
                f"правило {rule_id!r} названо дважды (same id) — журнал не различит"
            )
        rules.append(
            Rule(
                rule_id,
                _parse_when(rule_id, raw.get("when")),
                _parse_reply(rule_id, raw.get("reply")),
            )
        )
    return Scenario(name=str(data.get("name", "")), rules=tuple(rules))


def load_scenario(path: Path) -> Scenario:
    try:
        return parse_scenario(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        raise ScenarioError(f"{path}: не JSON ({error})") from error
