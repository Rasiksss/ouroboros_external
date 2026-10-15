"""Срез 31 (П3, П6): архитектурные стражи Ingress v2.

Владелец: «любое абсолютно любое сообщение пользователя должно сначала попадать
в атом парсинга на основе модели, а дальше МОДЕЛЬ должна решить». Срезы 29/29a/30
подменяли это решение кодом: регэкспы («извлеки/посчитай»), порог «≥5 кандидатов»,
ретраи разбора, period prepass. Возврат любого из них здесь краснеет.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "src" / "finengine" / "skills" / "production_runtime.py"
AGENT = REPO / "src" / "finengine" / "orchestrator" / "agent.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _identifiers(path: Path) -> set[str]:
    """Имена, реально используемые кодом (переменные, атрибуты, функции, импорты) —
    комментарии и docstring не считаются: страж смотрит на поведение, а не на текст."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_the_production_runtime_does_not_parse_or_regex_the_users_message():
    assert "re" not in _imports(RUNTIME), "регэкспы по тексту запроса — это решение кодом вместо модели"
    used = _identifiers(RUNTIME)
    for banned in ("parse_message", "parse_intent", "candidate_concepts", "_explicit_metric_operation",
                   "_retryable_metric_like", "_period_prepass_candidates", "raw_query"):
        assert banned not in used, f"{banned} вернулся в production_runtime — маршрут решает модель"


def test_the_agent_parses_every_message_before_any_document_or_dispatcher_step():
    source = AGENT.read_text(encoding="utf-8")
    parse_at = source.index("await parse_message(")
    assert parse_at < source.index("import execute_routed_goal"), "разбор — раньше исполнения конвейера"
    assert parse_at < source.index("async for ev in llm.stream_events("), "и раньше диспетчера"
    used = _identifiers(AGENT)
    assert "last_resolved_goal" not in used and "clarification" not in used, (
        "состояние уточнений периода снесено: его заменяет история диалога"
    )


def test_the_dispatcher_prompts_no_longer_default_to_the_freshest_period():
    """П6: по умолчанию — все доступные периоды отчёта."""
    for path in (
        AGENT,
        REPO / "src" / "finengine" / "llm" / "openclaw.py",
        REPO / "agent-workspace" / "skills" / "aifa-finance" / "SKILL.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert "самый свежий период" not in text and "самый свежий отчётный" not in text, path.name
        assert "период = самый свежий в отчёте" not in text, path.name


def test_no_period_clarification_code_remains_in_the_ingress():
    for path in (RUNTIME, AGENT):
        used = _identifiers(path)
        for banned in ("period_origin", "available_periods", "_clarification_snapshot"):
            assert banned not in used, f"{banned}: период больше не спрашивается"
        assert not any("уточните период" in text for text in _string_constants(path)), path.name
