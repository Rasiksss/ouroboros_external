import ast
import pathlib

import pytest

import ouroboros.app.multiuser_validation as validation
from ouroboros.app.multiuser_validation import (
    MultiuserConfigurationError,
    resolved_keyspace,
    validate_deployment_keyspace,
)
from ouroboros.storage.runtime_models import DeploymentKeyspace
from tests.helpers.conftest_base import refresh_test_config
from tests.helpers.deployment_keyspace import sql_deployment_keyspace_repository
from tests.helpers.sql_faults import fail_sql


def test_single_user_startup_does_not_access_the_repository(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "false")
    repository = sql_deployment_keyspace_repository()
    with fail_sql("deployment_keyspace", OSError("database unavailable"), expect_failure=False):
        validate_deployment_keyspace(repository)


def test_repository_failure_aborts_multiuser_startup(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    repository = sql_deployment_keyspace_repository()
    with fail_sql("deployment_keyspace", OSError("database unavailable")):
        with pytest.raises(OSError, match="database unavailable"):
            validate_deployment_keyspace(repository)


@pytest.fixture
def keyspace_settings(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", "ouroboros-shared")


def test_the_first_start_records_the_keyspace_and_an_agreeing_one_proceeds(keyspace_settings) -> None:

    repository = sql_deployment_keyspace_repository()

    validate_deployment_keyspace(repository)
    recorded = repository.get_deployment_keyspace()
    assert recorded is not None
    assert not resolved_keyspace().differs_from(recorded)
    validate_deployment_keyspace(repository)

    assert recorded.backend == "s3"
    assert recorded.key_prefix.startswith("tenants/")
    assert repository.record_deployment_keyspace(resolved_keyspace()).recorded_at == recorded.recorded_at, (
        "an agreeing start must rewrite nothing"
    )


def test_an_instance_with_another_prefix_is_refused_naming_both(monkeypatch, keyspace_settings) -> None:
    """A per-pod tenant id is a private keyspace inside a correctly shared bucket."""

    repository = sql_deployment_keyspace_repository()
    validate_deployment_keyspace(repository)

    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "pod-3")
    refresh_test_config()

    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_deployment_keyspace(repository)

    message = str(refusal.value)
    assert "tenants/pod-3" in message
    assert "recorded:" in message and "this instance:" in message
    assert "keyspace-record --replace" in message


def test_an_instance_with_another_bucket_is_refused(monkeypatch, keyspace_settings) -> None:

    repository = sql_deployment_keyspace_repository()
    validate_deployment_keyspace(repository)

    monkeypatch.setenv("OUROBOROS_S3_BUCKET", "somebody-elses-bucket")
    refresh_test_config()

    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_deployment_keyspace(repository)

    assert "somebody-elses-bucket" in str(refusal.value)
    assert "ouroboros-shared" in str(refusal.value)


def test_two_instances_at_different_endpoints_disagree(monkeypatch, keyspace_settings) -> None:
    """One bucket name at two endpoints is two buckets — the symptom this record catches."""

    monkeypatch.setenv("OUROBOROS_S3_ENDPOINT", "https://storage-a.example")
    refresh_test_config()
    repository = sql_deployment_keyspace_repository()
    validate_deployment_keyspace(repository)

    monkeypatch.setenv("OUROBOROS_S3_ENDPOINT", "https://storage-b.example")
    refresh_test_config()

    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_deployment_keyspace(repository)

    assert "storage-b.example" in str(refusal.value)


def test_the_replacement_step_is_the_only_way_to_move_a_recorded_keyspace(keyspace_settings) -> None:
    """A setting that skips the comparison would be a fail-open switch, and would be left on."""

    repository = sql_deployment_keyspace_repository()
    repository.record_deployment_keyspace(DeploymentKeyspace(backend="s3", bucket="b", key_prefix="tenants/old"))

    moved = repository.replace_deployment_keyspace(
        DeploymentKeyspace(backend="s3", bucket="b", key_prefix="tenants/new")
    )

    assert moved.key_prefix == "tenants/new"
    # The comparison reads exactly one setting — the mode itself. Anything else it branched
    # on would be a switch that turns a safety refusal off, and it would be left on.
    checked = ast.parse(pathlib.Path(validation.__file__).read_text(encoding="utf-8"))
    body = next(
        node
        for node in ast.walk(checked)
        if isinstance(node, ast.FunctionDef) and node.name == "validate_deployment_keyspace"
    )
    settings_read = {
        node.attr for node in ast.walk(body) if isinstance(node, ast.Attribute) and node.attr.startswith("OUROBOROS_")
    }
    assert settings_read == {"OUROBOROS_MULTIUSER"}, settings_read
