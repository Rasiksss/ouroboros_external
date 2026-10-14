from sqlalchemy import text
from sqlalchemy.engine import Engine, Row

from infra.sql_connection import sql_connection
from ouroboros.app.interfaces.deployment_keyspace import IDeploymentKeyspaceRepository
from ouroboros.storage.runtime_models import DeploymentKeyspace
from ouroboros.utils import utc_now_iso

_DEPLOYMENT_KEYSPACE_ROW = "keyspace"


class SqlDeploymentKeyspaceRepository(IDeploymentKeyspaceRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_deployment_keyspace(self, keyspace: DeploymentKeyspace) -> DeploymentKeyspace:
        """Record only if absent, then return the persisted keyspace."""
        stored = DeploymentKeyspace(
            backend=keyspace.backend,
            bucket=keyspace.bucket,
            endpoint=keyspace.endpoint,
            key_prefix=keyspace.key_prefix,
            recorded_at=keyspace.recorded_at or utc_now_iso(),
        )
        with sql_connection(self._engine) as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO deployment_keyspace (id, backend, bucket, endpoint, key_prefix, recorded_at)
                    VALUES (:id, :backend, :bucket, :endpoint, :key_prefix, :recorded_at)
                    ON CONFLICT(id) DO NOTHING
                    """
                ),
                {
                    "id": _DEPLOYMENT_KEYSPACE_ROW,
                    "backend": stored.backend,
                    "bucket": stored.bucket,
                    "endpoint": stored.endpoint,
                    "key_prefix": stored.key_prefix,
                    "recorded_at": stored.recorded_at,
                },
            )
            conn.commit()
            row = conn.execute(
                text("SELECT * FROM deployment_keyspace WHERE id=:id"),
                {"id": _DEPLOYMENT_KEYSPACE_ROW},
            ).fetchone()
        if row is None:
            # The insert above either wrote it or found it, so the only way here is a
            # concurrent delete — which nothing in the tree does.
            raise RuntimeError("the deployment keyspace disappeared between writing and reading it")
        return self._row_to_keyspace(row)

    @staticmethod
    def _row_to_keyspace(row: Row[tuple[object, ...]]) -> DeploymentKeyspace:
        mapping = row._mapping
        return DeploymentKeyspace(
            backend=mapping["backend"],
            bucket=mapping["bucket"],
            endpoint=mapping["endpoint"],
            key_prefix=mapping["key_prefix"],
            recorded_at=mapping["recorded_at"],
        )

    def get_deployment_keyspace(self) -> DeploymentKeyspace | None:
        """Read the recorded keyspace without writing it."""
        with sql_connection(self._engine) as conn:
            row = conn.execute(
                text("SELECT * FROM deployment_keyspace WHERE id=:id"),
                {"id": _DEPLOYMENT_KEYSPACE_ROW},
            ).fetchone()
        return self._row_to_keyspace(row) if row else None

    def replace_deployment_keyspace(self, keyspace: DeploymentKeyspace) -> DeploymentKeyspace:
        """Update or insert without a delete gap, then return the persisted keyspace."""
        stored = DeploymentKeyspace(
            backend=keyspace.backend,
            bucket=keyspace.bucket,
            endpoint=keyspace.endpoint,
            key_prefix=keyspace.key_prefix,
            recorded_at=keyspace.recorded_at or utc_now_iso(),
        )
        params = {
            "id": _DEPLOYMENT_KEYSPACE_ROW,
            "backend": stored.backend,
            "bucket": stored.bucket,
            "endpoint": stored.endpoint,
            "key_prefix": stored.key_prefix,
            "recorded_at": stored.recorded_at,
        }
        with sql_connection(self._engine) as conn:
            applied = conn.execute(
                text(
                    """
                    UPDATE deployment_keyspace
                    SET backend=:backend, bucket=:bucket, endpoint=:endpoint,
                        key_prefix=:key_prefix, recorded_at=:recorded_at
                    WHERE id=:id
                    """
                ),
                params,
            ).rowcount
            if not applied:
                conn.execute(
                    text(
                        """
                        INSERT INTO deployment_keyspace (id, backend, bucket, endpoint, key_prefix, recorded_at)
                        VALUES (:id, :backend, :bucket, :endpoint, :key_prefix, :recorded_at)
                        ON CONFLICT(id) DO NOTHING
                        """
                    ),
                    params,
                )
            conn.commit()
            row = conn.execute(
                text("SELECT * FROM deployment_keyspace WHERE id=:id"),
                {"id": _DEPLOYMENT_KEYSPACE_ROW},
            ).fetchone()
        if row is None:
            raise RuntimeError("the deployment keyspace disappeared between writing and reading it")
        return self._row_to_keyspace(row)
