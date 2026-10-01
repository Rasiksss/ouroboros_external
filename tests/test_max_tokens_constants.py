"""Regression tests for routed token budgets and tool timeouts."""

from unittest.mock import MagicMock


def test_context_compaction_passes_32k_token_budget(monkeypatch):
    from ouroboros.agent import context_compaction

    llm = MagicMock()
    llm.chat.return_value = ({"content": "[round:1]\nsummary"}, {})
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: llm)

    summaries, _usage = context_compaction._summarize_round_batch([(1, "content")])

    assert summaries == {1: "summary"}
    assert llm.chat.call_args.kwargs["max_tokens"] == 32_768


def test_tool_timeout_per_tool_minimum_wins_when_higher():
    """_get_tool_timeout must retain a higher per-tool minimum."""
    from unittest.mock import patch

    import ouroboros.agent.loop_tool_execution as mod
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    with patch(
        "ouroboros.storage.settings.store.load_settings",
        return_value=type("S", (), {"OUROBOROS_TOOL_TIMEOUT_SEC": 120})(),
    ):
        result = mod.ToolCallExecutor._get_tool_timeout(resolve_tool_runtime_metadata("run_shell"))
    assert result == 360


def test_tool_timeout_settings_wins_when_higher():
    """_get_tool_timeout: if settings > per_tool, settings wins."""
    from unittest.mock import patch

    import ouroboros.agent.loop_tool_execution as mod
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    with patch(
        "ouroboros.storage.settings.store.load_settings",
        return_value=type("S", (), {"OUROBOROS_TOOL_TIMEOUT_SEC": 900})(),
    ):
        result = mod.ToolCallExecutor._get_tool_timeout(resolve_tool_runtime_metadata("run_shell"))
    assert result == 900


def test_tool_timeout_uses_max_of_settings_and_per_tool():
    """_get_tool_timeout must return max(settings, per_tool) not just settings."""
    from unittest.mock import patch

    import ouroboros.agent.loop_tool_execution as mod
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    with patch(
        "ouroboros.storage.settings.store.load_settings",
        return_value=type("S", (), {"OUROBOROS_TOOL_TIMEOUT_SEC": 600})(),
    ):
        result = mod.ToolCallExecutor._get_tool_timeout(resolve_tool_runtime_metadata("run_shell"))
    assert result == 600, f"Expected 600 (settings), got {result}"
