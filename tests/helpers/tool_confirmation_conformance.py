from abc import ABC, abstractmethod

from ouroboros.storage.runtime_models import ToolConfirmation
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


def confirmation(**fields):
    return ToolConfirmation(**{**TEST_SCOPE.to_dict(), "confirmation_id": "c1", "task_id": "task", **fields})


class ToolConfirmationReaderConformance(ABC):
    @abstractmethod
    def make_reader(self, rows):
        pass

    def test_missing_reads_are_empty(self):
        reader = self.make_reader([])
        assert reader.read_tool_confirmation_status("missing") == ""
        assert reader.read_open_tool_confirmation(TEST_SCOPE) is None
        assert reader.read_tool_confirmation_decisions("missing", round_idx=1) == {}

    @parametrize_from_dict({axis: {"axis": axis} for axis in TEST_SCOPE.to_dict()})
    def test_open_request_requires_every_scope_axis(self, axis):
        request = confirmation(tool_name="tool", call_ordinal=2, round_idx=3, args_hash="hash", created_at="now")
        reader = self.make_reader([(request, "")])
        assert reader.read_open_tool_confirmation(TEST_SCOPE) == request
        other = "dialogs" if axis == "surface_id" else "other"
        assert reader.read_open_tool_confirmation(scope_with(TEST_SCOPE, **{axis: other})) is None

    @parametrize_from_dict({s: {"status": s} for s in ["approved", "denied", "expired"]})
    def test_status_and_open_are_distinct(self, status):
        reader = self.make_reader([(confirmation(status=status), "")])
        assert reader.read_tool_confirmation_status("c1") == status
        assert reader.read_open_tool_confirmation(TEST_SCOPE) is None

    def test_decisions_select_unspent_nonpending_rows_of_one_task_and_round(self):
        rows = [
            (
                confirmation(
                    confirmation_id="approved",
                    status="approved",
                    round_idx=2,
                    call_ordinal=1,
                    tool_name="a",
                    args_hash="h1",
                ),
                "",
            ),
            (
                confirmation(
                    confirmation_id="denied",
                    status="denied",
                    round_idx=2,
                    call_ordinal=2,
                    tool_name="a",
                    args_hash="h2",
                ),
                "",
            ),
            (
                confirmation(
                    confirmation_id="expired",
                    status="expired",
                    round_idx=2,
                    call_ordinal=3,
                    tool_name="b",
                    args_hash="h3",
                ),
                "",
            ),
            (confirmation(confirmation_id="spent", status="approved", round_idx=2, call_ordinal=4), "now"),
            (confirmation(confirmation_id="pending", round_idx=2, call_ordinal=5), ""),
            (confirmation(confirmation_id="other-round", status="approved", round_idx=1), ""),
            (confirmation(confirmation_id="other-task", status="approved", round_idx=2, task_id="other"), ""),
        ]
        reader = self.make_reader(rows)
        expected = {(1, "a", "h1"): "approved", (2, "a", "h2"): "denied", (3, "b", "h3"): "expired"}
        assert reader.read_tool_confirmation_decisions("task", round_idx=2) == expected
        assert reader.read_tool_confirmation_decisions("", round_idx=2) == {}
        assert reader.read_tool_confirmation_decisions("task", round_idx=2) == expected

    def test_returned_request_does_not_alias_storage(self):
        request = confirmation()
        reader = self.make_reader([(request, "")])
        reader.read_open_tool_confirmation(TEST_SCOPE).tool_name = "changed"
        assert reader.read_open_tool_confirmation(TEST_SCOPE) == request
