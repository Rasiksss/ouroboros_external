from abc import ABC, abstractmethod

from ouroboros.storage.runtime_models import DeploymentKeyspace


class IDeploymentKeyspaceRepository(ABC):
    @abstractmethod
    def record_deployment_keyspace(self, keyspace: DeploymentKeyspace) -> DeploymentKeyspace:
        pass

    @abstractmethod
    def get_deployment_keyspace(self) -> DeploymentKeyspace | None:
        pass

    @abstractmethod
    def replace_deployment_keyspace(self, keyspace: DeploymentKeyspace) -> DeploymentKeyspace:
        pass
