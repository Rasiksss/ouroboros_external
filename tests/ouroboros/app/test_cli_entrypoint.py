from __future__ import annotations

import sys
from io import BytesIO
from types import SimpleNamespace

import pytest
from botocore.client import BaseClient

from ouroboros.app import cli
from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
from ouroboros.app.multiuser_validation import resolved_keyspace
from ouroboros.observability import aef_tracing
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict


def test_server_subcommand_sanitizes_argv(monkeypatch):
    seen = {}

    class FakeServer:
        @staticmethod
        def main():
            seen["argv"] = list(sys.argv)
            return 0

    monkeypatch.setitem(sys.modules, "server", FakeServer)
    monkeypatch.setattr(sys, "argv", ["ouroboros", "server", "--host", "127.0.0.1", "--port", "9000"])
    monkeypatch.delenv("OUROBOROS_ENV", raising=False)

    result = cli._server_command(SimpleNamespace(host="127.0.0.1", port=9000, no_ui=True))

    assert result == 0
    assert seen["argv"] == ["ouroboros"]
    assert sys.argv == ["ouroboros", "server", "--host", "127.0.0.1", "--port", "9000"]
    assert cli.os.environ["OUROBOROS_ENV"] == "production"


@parametrize_from_dict(
    {
        "empty": {"initial": "empty", "exit_code": 0},
        "matching": {"initial": "matching", "exit_code": 0},
        "different": {"initial": "different", "exit_code": 1},
    }
)
def test_keyspace_diagnostic_reports_without_changing_the_record(capsys, initial: str, exit_code: int) -> None:
    repository = SqlDeploymentKeyspaceRepository(runtime_persistence.current.engine)
    recorded = None
    if initial != "empty":
        value = resolved_keyspace()
        if initial == "different":
            value.key_prefix = "another-deployment"
        recorded = repository.record_deployment_keyspace(value)

    assert cli.main(["keyspace-record"]) == exit_code

    output = capsys.readouterr().out
    assert "this process:" in output
    assert "recorded:" in output
    if recorded is None:
        assert "nothing yet" in output
    else:
        assert recorded.describe() in output
    assert repository.get_deployment_keyspace() == recorded


@parametrize_from_dict({"empty": {"existing": False}, "existing": {"existing": True}})
def test_keyspace_replace_records_the_resolved_keyspace(capsys, existing: bool) -> None:
    repository = SqlDeploymentKeyspaceRepository(runtime_persistence.current.engine)
    if existing:
        repository.record_deployment_keyspace(resolved_keyspace().model_copy(update={"key_prefix": "old"}))

    assert cli.main(["keyspace-record", "--replace"]) == 0

    stored = repository.get_deployment_keyspace()
    assert stored is not None
    assert not resolved_keyspace().differs_from(stored)
    assert capsys.readouterr().out == f"recorded keyspace is now: {stored.describe()}\n"


@pytest.mark.parametrize("invalid_record", [False, True])
def test_backfill_command_reports_skips_without_failing_and_traces_storage(monkeypatch, capsys, invalid_record):
    from ouroboros.app.cli import main

    traces = []

    def empty_bucket(self, operation, parameters):
        traces.append(aef_tracing.current_trace_id())
        if invalid_record:
            if operation == "ListObjectsV2" and "StartAfter" not in parameters:
                return {"Contents": [{"Key": parameters["Prefix"] + "_meta/bad.json"}]}
            if operation == "GetObject":
                return {"Body": BytesIO(b"{")}
        return {}

    monkeypatch.setenv("OUROBOROS_S3_ACCESS_KEY", "test")
    monkeypatch.setenv("OUROBOROS_S3_SECRET_KEY", "test")
    monkeypatch.setattr(BaseClient, "_make_api_call", empty_bucket)
    assert main(["file-catalog-backfill", "--owner-id", "global", "--agent-id", "main"]) == 0
    assert traces and all(aef_tracing.normalize_trace_id(trace) == traces[0] != "" for trace in traces)
    assert aef_tracing.current_trace_id() == ""
    assert f'"skipped": {int(invalid_record)}' in capsys.readouterr().out


def test_backfill_command_does_not_report_success_when_s3_is_unavailable(monkeypatch, capsys):
    from ouroboros.app.cli import main

    def unavailable(*args, **kwargs):
        raise OSError("S3 unavailable")

    monkeypatch.setenv("OUROBOROS_S3_ACCESS_KEY", "test")
    monkeypatch.setenv("OUROBOROS_S3_SECRET_KEY", "test")
    monkeypatch.setattr(BaseClient, "_make_api_call", unavailable)
    with pytest.raises(OSError, match="S3 unavailable"):
        main(["file-catalog-backfill", "--owner-id", "global", "--agent-id", "main", "--apply"])
    assert '"imported"' not in capsys.readouterr().out
