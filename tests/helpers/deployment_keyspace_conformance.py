from abc import ABC, abstractmethod

from ouroboros.app.interfaces.deployment_keyspace import IDeploymentKeyspaceRepository
from ouroboros.storage.runtime_models import DeploymentKeyspace
from tests.helpers.parametrize import parametrize_from_dict

RECORDED_AT = "2026-01-01T00:00:00+00:00"


class DeploymentKeyspaceRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IDeploymentKeyspaceRepository:
        pass

    def test_reading_an_empty_repository_does_not_record_a_keyspace(self) -> None:
        repository = self.make_repository()
        assert repository.get_deployment_keyspace() is None
        assert repository.get_deployment_keyspace() is None

    def test_first_record_wins_including_its_timestamp(self) -> None:
        repository = self.make_repository()
        first = DeploymentKeyspace(backend="s3", bucket="first", key_prefix="one", recorded_at=RECORDED_AT)
        later = DeploymentKeyspace(backend="s3", bucket="later", key_prefix="two")

        assert repository.record_deployment_keyspace(first) == first
        assert repository.record_deployment_keyspace(later) == first
        assert repository.get_deployment_keyspace() == first

    @parametrize_from_dict({"record": {"replace": False}, "replace": {"replace": True}})
    def test_writes_fill_timestamp_without_mutating_the_input(self, replace: bool) -> None:
        repository = self.make_repository()
        value = DeploymentKeyspace(backend="s3", bucket="bucket", endpoint="https://storage.example", key_prefix="one")
        write = repository.replace_deployment_keyspace if replace else repository.record_deployment_keyspace

        stored = write(value)

        assert stored.recorded_at
        assert value.recorded_at == ""
        assert stored is not value
        assert stored == value.model_copy(update={"recorded_at": stored.recorded_at})
        assert repository.get_deployment_keyspace() == stored

    @parametrize_from_dict({"empty": {"existing": False}, "existing": {"existing": True}})
    def test_replace_persists_all_fields_and_explicit_timestamp(self, existing: bool) -> None:
        repository = self.make_repository()
        if existing:
            repository.record_deployment_keyspace(DeploymentKeyspace(backend="old", bucket="old", key_prefix="old"))
        value = DeploymentKeyspace(
            backend="s3", bucket="new", endpoint="https://new.example", key_prefix="new", recorded_at=RECORDED_AT
        )

        assert repository.replace_deployment_keyspace(value) == value
        assert repository.get_deployment_keyspace() == value

    def test_callers_cannot_mutate_the_stored_snapshot(self) -> None:
        repository = self.make_repository()
        value = DeploymentKeyspace(backend="s3", bucket="original", key_prefix="one", recorded_at=RECORDED_AT)
        expected = value.model_copy(deep=True)
        stored = repository.record_deployment_keyspace(value)
        value.bucket = "input changed"
        stored.bucket = "result changed"
        read = repository.get_deployment_keyspace()
        assert read is not None
        read.bucket = "read changed"

        assert repository.get_deployment_keyspace() == expected
