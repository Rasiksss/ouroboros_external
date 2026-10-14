from abc import ABC, abstractmethod

import pytest

from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_models import ToolInvocation
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


class ToolInvocationRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IExecutionMemoryRepository:
        pass

    def test_record_round_trips_every_field_and_returns_the_supplied_object(self):
        repository = self.make_repository()
        invocation = ToolInvocation(
            **TEST_SCOPE.to_dict(),
            task_id="task",
            tool_name="calculator",
            status="ok",
            args_redacted={"expression": "1+1"},
            result_summary={"answer": "2"},
            result_index={"entities": {"number": "2"}},
            search_terms=["number", "число"],
            payload={"extra": [1, 2]},
            payload_hash="hash",
            sensitivity="private",
            retention_class="raw",
            expires_at="2027-01-01",
        )

        assert repository.record_tool_invocation(invocation) is invocation
        assert invocation.tool_invocation_id and invocation.created_at
        assert repository.list_tool_invocations(task_id="task") == [invocation]

    def test_conflict_replaces_the_whole_invocation_including_task_and_scope(self):
        repository = self.make_repository()
        original = ToolInvocation(**TEST_SCOPE.to_dict(), tool_invocation_id="same", task_id="old")
        repository.record_tool_invocation(original)
        replacement = ToolInvocation(
            owner_id="other",
            agent_id="other",
            surface_id="dialogs",
            chat_id="other",
            tool_invocation_id="same",
            task_id="new",
            tool_name="lookup",
            status="error",
            args_redacted={"new": 1},
            result_summary={"error": "failed"},
            result_index={"id": "new"},
            search_terms=["new"],
            payload={"new": True},
            payload_hash="new",
            sensitivity="private",
            retention_class="raw",
            created_at="2026-09-22",
            expires_at="2026-10-22",
        )
        repository.record_tool_invocation(replacement)

        assert repository.list_tool_invocations(task_id="old") == []
        assert repository.list_tool_invocations(task_id="new") == [replacement]

    @parametrize_from_dict(
        {
            "two": {"limit": 2, "expected": ["c", "b"]},
            "all": {"limit": 100, "expected": ["c", "b", "a"]},
            "zero": {"limit": 0, "expected": ["c"]},
            "negative": {"limit": -2, "expected": ["c"]},
        }
    )
    def test_read_orders_by_time_then_id_and_clamps_limit(self, limit, expected):
        repository = self.make_repository()
        for ident, timestamp, task in [("b", "2", "mine"), ("a", "1", "mine"), ("c", "2", "mine"), ("z", "3", "other")]:
            repository.record_tool_invocation(
                ToolInvocation(
                    **TEST_SCOPE.to_dict(),
                    tool_invocation_id=ident,
                    task_id=task,
                    created_at=timestamp,
                )
            )
        assert [
            item.tool_invocation_id for item in repository.list_tool_invocations(task_id="mine", limit=limit)
        ] == expected

    def test_empty_task_id_is_a_predicate_and_task_id_is_required(self):
        repository = self.make_repository()
        for task in ["mine", ""]:
            repository.record_tool_invocation(ToolInvocation(**TEST_SCOPE.to_dict(), task_id=task))
        assert [item.task_id for item in repository.list_tool_invocations(task_id="")] == [""]
        with pytest.raises(TypeError):
            repository.list_tool_invocations()  # type: ignore[call-arg]

    def test_read_and_write_do_not_alias_stored_payloads(self):
        repository = self.make_repository()
        invocation = ToolInvocation(**TEST_SCOPE.to_dict(), task_id="task", payload={"items": [1]})
        repository.record_tool_invocation(invocation)
        invocation.payload["items"].append(2)
        returned = repository.list_tool_invocations(task_id="task")[0]
        assert returned.payload == {"items": [1]}
        returned.payload["items"].append(3)
        assert repository.list_tool_invocations(task_id="task")[0].payload == {"items": [1]}
