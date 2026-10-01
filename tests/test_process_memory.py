"""Tests for process memory infrastructure.

Covers:
- Reflection loading into context (must stay excluded)
"""

import json

# ─────────────────────────────────────────────────────────────────────────────
# Reflection context loading
# ─────────────────────────────────────────────────────────────────────────────


class TestReflectionContextLoading:
    """Execution reflections are internal diagnostics and must not enter prompt context."""

    def test_reflections_not_loaded_into_recent_prompt_context(self, tmp_path):
        from ouroboros.agent.context import build_llm_messages
        from ouroboros.observability.event_record import record_event
        from ouroboros.storage.runtime_store import init_runtime_store
        from tests.helpers.owner_state import seed_owner_state

        class FakeEnv:
            def drive_path(self, p):
                return tmp_path / p

            def repo_path(self, p):
                return tmp_path / "repo" / p

            @property
            def repo_dir(self):
                return tmp_path / "repo"

            @property
            def drive_root(self):
                return tmp_path

        (tmp_path / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "prompts" / "SYSTEM.md").write_text("System prompt", encoding="utf-8")
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        init_runtime_store(tmp_path)
        seed_owner_state(tmp_path, spent_calls=0)
        record_event(
            tmp_path,
            "task_reflections",
            "task_reflection",
            {"task_id": "task-1", "reflection": "PRIVATE_REFLECTION_MARKER"},
        )

        messages, _ = build_llm_messages(
            env=FakeEnv(),
            task={"id": "task-1", "type": "task", "text": "hello"},
        )

        assert "PRIVATE_REFLECTION_MARKER" not in json.dumps(messages, ensure_ascii=False)
