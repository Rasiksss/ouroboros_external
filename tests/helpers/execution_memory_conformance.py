from abc import ABC, abstractmethod

import pytest

from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_models import TaskEpisode, ToolInvocation
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


class ExecutionMemoryRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IExecutionMemoryRepository: ...

    def test_episode_round_trip_and_generated_identity(self):
        repo = self.make_repository()
        episode = TaskEpisode(
            **TEST_SCOPE.to_dict(),
            task_id="task",
            user_text="question",
            answer_preview="answer",
            tools_used=["lookup"],
            domains=["docs"],
            entities={"project": "Orion"},
            refs={"document": "42"},
            search_terms=["orion"],
            payload={"extra": [1, 2]},
            expires_at="2027",
        )
        assert repo.record_task_episode(episode) == episode
        assert episode.episode_id and episode.created_at
        assert repo.list_task_episodes(scope=TEST_SCOPE) == [episode]

    @parametrize_from_dict({"ordinary update": {"logical": False}, "logical replay": {"logical": True}})
    def test_episode_conflict_preserves_logical_first_write(self, logical):
        repo = self.make_repository()
        original = TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="episode", payload={"logical_job": logical})
        repo.record_task_episode(original)
        replacement = original.model_copy(update={"user_text": "changed", "created_at": "later"})
        expected = original if logical else replacement
        assert repo.record_task_episode(replacement) == expected
        assert repo.list_task_episodes(scope=TEST_SCOPE) == [expected]

    @parametrize_from_dict({axis: {"axis": axis} for axis in ("owner_id", "agent_id", "surface_id", "chat_id")})
    def test_episode_scope_isolation(self, axis):
        repo = self.make_repository()
        other = scope_with(TEST_SCOPE, **{axis: "dialogs" if axis == "surface_id" else "other"})
        mine = TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="mine")
        repo.record_task_episode(mine)
        repo.record_task_episode(TaskEpisode(**other.to_dict(), episode_id="other"))
        assert repo.list_task_episodes(scope=TEST_SCOPE) == [mine]

    @parametrize_from_dict(
        {
            "case insensitive substring": {"domains": [" DOC "], "terms": ["RI"], "expected": ["c", "a"]},
            "OR within each filter": {
                "domains": ["absent", "docs"],
                "terms": ["none", "orion"],
                "expected": ["c", "a"],
            },
            "AND across filters": {"domains": ["weather"], "terms": ["orion"], "expected": []},
            "empty filters": {"domains": [" "], "terms": [], "expected": ["c", "b", "a"]},
            "SQL wildcard": {"domains": ["d_cs"], "terms": ["o%"], "expected": ["c", "a"]},
        }
    )
    def test_episode_filters_and_stable_order(self, domains, terms, expected):
        repo = self.make_repository()
        for ident, domain, term in [("a", "docs", "orion"), ("b", "weather", "rain"), ("c", "docs", "orion")]:
            repo.record_task_episode(
                TaskEpisode(
                    **TEST_SCOPE.to_dict(), episode_id=ident, created_at="same", domains=[domain], search_terms=[term]
                )
            )
        assert [
            row.episode_id for row in repo.list_task_episodes(scope=TEST_SCOPE, domains=domains, search_terms=terms)
        ] == expected

    def test_snapshot_version_filters_after_limit_without_refill(self):
        repo = self.make_repository()
        older = repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="a", created_at="1"))
        newer = repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="b", created_at="2"))
        snapshot = {row.episode_id: row.snapshot_version() for row in (older, newer)}
        repo.record_task_episode(newer.model_copy(update={"answer_preview": "changed"}))
        assert repo.list_task_episodes(scope=TEST_SCOPE, snapshot=snapshot, limit=1) == []
        assert repo.list_task_episodes(scope=TEST_SCOPE, snapshot=snapshot) == [older]
        assert repo.list_task_episodes(scope=TEST_SCOPE, snapshot={}) == []

    def test_outer_rollback_restores_both_record_types(self):
        repo = self.make_repository()
        with pytest.raises(ValueError, match="abort"):
            with repo.transaction:
                repo.record_tool_invocation(ToolInvocation(**TEST_SCOPE.to_dict(), task_id="task"))
                repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="episode"))
                raise ValueError("abort")
        assert repo.list_task_episodes(scope=TEST_SCOPE) == []
        assert repo.list_tool_invocations(task_id="task") == []

    def test_nested_rollback_keeps_outer_writes(self):
        repo = self.make_repository()
        with repo.transaction:
            first = repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="first", created_at="1"))
            with pytest.raises(ValueError, match="nested"):
                with repo.transaction:
                    repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="lost"))
                    repo.record_tool_invocation(ToolInvocation(**TEST_SCOPE.to_dict(), task_id="task"))
                    raise ValueError("nested")
            last = repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="last", created_at="2"))
        assert repo.list_task_episodes(scope=TEST_SCOPE) == [last, first]
        assert repo.list_tool_invocations(task_id="task") == []
